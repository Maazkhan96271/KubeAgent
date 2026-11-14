"""Incident ingestion, analysis and human-in-the-loop approval endpoints.

The store is intentionally in-memory: it keeps the 50 most recent incidents,
publishes every state change to per-incident SSE subscribers, and is guarded by
a re-entrant lock so it is safe to share between the event loop and worker
threads.  Swap :class:`IncidentStore` for a database-backed repository without
touching the routers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.orchestrator import AgentOrchestrator
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/incidents", tags=["incidents"])

#: Channel that receives a copy of every incident event (used by the global stream).
GLOBAL_CHANNEL = "*"


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class IncidentStatus(str, Enum):
    """Lifecycle of an incident.

    ``AWAITING_APPROVAL`` means the agent has produced remediation steps and a
    human must sign off before anything is executed.
    """

    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RESOLVED = "RESOLVED"


_SEVERITY_BY_LABEL: dict[str, Severity] = {
    "critical": Severity.critical,
    "page": Severity.critical,
    "fatal": Severity.critical,
    "high": Severity.high,
    "error": Severity.high,
    "warning": Severity.medium,
    "medium": Severity.medium,
    "low": Severity.low,
    "info": Severity.info,
}


def _severity_from(value: str | None, default: Severity = Severity.medium) -> Severity:
    if not value:
        return default
    return _SEVERITY_BY_LABEL.get(value.strip().lower(), default)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class IncidentCreate(BaseModel):
    """Payload accepted when an incident is reported manually."""

    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=3, max_length=8000)
    namespace: str = Field(default="default", max_length=253)
    workload: str | None = Field(
        default=None, max_length=253, description="Deployment or pod name, when known."
    )
    severity: Severity = Severity.medium
    source: str = Field(default="manual", max_length=64)
    labels: dict[str, str] = Field(default_factory=dict)


class ApprovalRecord(BaseModel):
    """Audit trail for a human sign-off."""

    approved_by: str
    approved_at: datetime = Field(default_factory=utcnow)
    notes: str | None = None
    approved_actions: list[str] = Field(default_factory=list)


class Diagnosis(BaseModel):
    """Structured agent output for one incident."""

    summary: str
    root_cause: str
    severity: Severity
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    #: True when the proposed remediation must be approved by a human.
    requires_human_approval: bool = True
    generated_by: str = "rules:builtin"
    created_at: datetime = Field(default_factory=utcnow)


class Incident(IncidentCreate):
    """A stored incident record."""

    id: str
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime
    updated_at: datetime
    diagnosis: Diagnosis | None = None
    approval: ApprovalRecord | None = None


class IncidentEvent(BaseModel):
    """One entry of an incident's event log, also delivered over SSE."""

    id: str = Field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:10]}")
    incident_id: str
    type: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class AlertmanagerAlert(BaseModel):
    """A single alert inside an Alertmanager v4 webhook payload."""

    model_config = ConfigDict(extra="allow")

    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None


class WebhookRequest(BaseModel):
    """Accepts an Alertmanager v4 webhook or a simple custom alert payload.

    Alertmanager form (``alerts`` present)::

        {"status": "firing", "alerts": [{"labels": {...}, "annotations": {...}}]}

    Custom form (``title`` present)::

        {"title": "...", "description": "...", "namespace": "default"}
    """

    model_config = ConfigDict(populate_by_name=True)

    # Alertmanager v4
    version: str | None = None
    group_key: str | None = Field(default=None, alias="groupKey")
    status: str | None = None
    receiver: str | None = None
    alerts: list[AlertmanagerAlert] | None = None
    common_labels: dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    common_annotations: dict[str, str] = Field(
        default_factory=dict, alias="commonAnnotations"
    )
    external_url: str | None = Field(default=None, alias="externalURL")

    # Custom alert
    title: str | None = None
    description: str | None = None
    namespace: str | None = None
    workload: str | None = None
    severity: Severity | None = None
    source: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_alert_or_title(self) -> "WebhookRequest":
        if not self.alerts and not self.title:
            raise ValueError("webhook payload must contain 'alerts' (Alertmanager) or 'title' (custom)")
        return self


class WebhookIngestResponse(BaseModel):
    created: list[Incident] = Field(default_factory=list)
    created_count: int = 0
    skipped: int = 0
    message: str = ""


class SimulateRequest(BaseModel):
    """Trigger one of the built-in chaos scenarios."""

    scenario: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description=(
            "Scenario name or alias: OOMKilled, CPU Spike, Config Error "
            "(also accepts the full template names)."
        ),
    )
    auto_analyze: bool = Field(
        default=False,
        description="Start the agent analysis in the background so the SSE stream shows it live.",
    )


class SimulateResponse(BaseModel):
    incident: Incident
    scenario: dict[str, Any]
    analysis_started: bool = False


class ApproveRequest(BaseModel):
    """Human-in-the-loop approval for the pending remediation."""

    approved_by: str = Field(..., min_length=2, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------
MAX_INCIDENTS = 50
MAX_EVENTS_PER_INCIDENT = 100


class IncidentStore:
    """Thread-safe ring of recent incidents plus a per-incident event bus."""

    def __init__(
        self, max_incidents: int = MAX_INCIDENTS, max_events: int = MAX_EVENTS_PER_INCIDENT
    ) -> None:
        self._max_incidents = max_incidents
        self._max_events = max_events
        self._incidents: dict[str, Incident] = {}
        self._order: deque[str] = deque()
        self._events: dict[str, deque[IncidentEvent]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[IncidentEvent]]] = {}
        self._lock = threading.RLock()

    # -- queries -------------------------------------------------------
    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            return self._incidents.get(incident_id)

    def list(
        self,
        status_filter: IncidentStatus | None = None,
        limit: int = 50,
    ) -> list[Incident]:
        with self._lock:
            items = sorted(
                self._incidents.values(),
                key=lambda item: item.updated_at,
                reverse=True,
            )
        if status_filter is not None:
            items = [item for item in items if item.status is status_filter]
        return items[: max(limit, 1)]

    def find_open_by_fingerprint(self, fingerprint: str) -> Incident | None:
        """Return an unresolved incident already tracked for this alert fingerprint."""
        if not fingerprint:
            return None
        with self._lock:
            for incident in self._incidents.values():
                if (
                    incident.labels.get("fingerprint") == fingerprint
                    and incident.status is not IncidentStatus.RESOLVED
                ):
                    return incident
        return None

    # -- mutations -----------------------------------------------------
    def create(self, payload: IncidentCreate) -> Incident:
        now = utcnow()
        incident = Incident(
            id=f"inc-{uuid.uuid4().hex[:12]}",
            status=IncidentStatus.OPEN,
            created_at=now,
            updated_at=now,
            **payload.model_dump(),
        )
        evicted: list[str] = []
        with self._lock:
            self._incidents[incident.id] = incident
            self._order.append(incident.id)
            self._events[incident.id] = deque(maxlen=self._max_events)
            while len(self._order) > self._max_incidents:
                evicted.append(self._order.popleft())
            for old_id in evicted:
                self._incidents.pop(old_id, None)
                self._events.pop(old_id, None)
                self._subscribers.pop(old_id, None)

        self.publish(incident.id, "incident.created", message=incident.title)
        return incident

    def set_status(
        self, incident_id: str, new_status: IncidentStatus, reason: str | None = None
    ) -> Incident:
        incident = self._patch(incident_id, status=new_status)
        self.publish(
            incident_id,
            "status",
            message=f"{incident.status.value}{f' ({reason})' if reason else ''}",
            data={"status": incident.status.value, "reason": reason},
        )
        return incident

    def set_diagnosis(
        self,
        incident_id: str,
        diagnosis: Diagnosis,
        final_status: IncidentStatus = IncidentStatus.AWAITING_APPROVAL,
    ) -> Incident:
        return self._patch(
            incident_id, diagnosis=diagnosis, status=final_status
        )

    def set_approval(self, incident_id: str, approval: ApprovalRecord) -> Incident:
        return self._patch(
            incident_id, approval=approval, status=IncidentStatus.RESOLVED
        )

    def _patch(self, incident_id: str, **changes: Any) -> Incident:
        with self._lock:
            incident = self._incidents.get(incident_id)
            if incident is None:
                raise KeyError(incident_id)
            updated = incident.model_copy(
                update={**changes, "updated_at": utcnow()}
            )
            self._incidents[incident_id] = updated
        return updated

    # -- event bus -----------------------------------------------------
    def publish(
        self,
        incident_id: str,
        event_type: str,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> IncidentEvent:
        event = IncidentEvent(
            incident_id=incident_id,
            type=event_type,
            message=message,
            data=data or {},
        )
        with self._lock:
            history = self._events.get(incident_id)
            if history is not None:
                history.append(event)
            channels = (incident_id, GLOBAL_CHANNEL)
            subscribers = [
                queue
                for channel in channels
                for queue in self._subscribers.get(channel, ())
            ]

        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("Dropping SSE event for a slow subscriber")
        return event

    def history(self, incident_id: str) -> list[IncidentEvent]:
        with self._lock:
            return list(self._events.get(incident_id, ()))

    @asynccontextmanager
    async def subscription(
        self, channel: str
    ) -> AsyncIterator[asyncio.Queue[IncidentEvent]]:
        queue: asyncio.Queue[IncidentEvent] = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.setdefault(channel, set()).add(queue)
        try:
            yield queue
        finally:
            with self._lock:
                subscribers = self._subscribers.get(channel)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(channel, None)


#: Process-wide store; replaced by a repository in a persistent deployment.
store = IncidentStore()


@lru_cache(maxsize=1)
def get_orchestrator() -> AgentOrchestrator:
    """FastAPI dependency returning a shared orchestrator (and its simulator)."""
    return AgentOrchestrator(get_settings())


def _require(incident_id: str) -> Incident:
    incident = store.get(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found",
        )
    return incident


# ---------------------------------------------------------------------------
# Background analysis
# ---------------------------------------------------------------------------
_background_tasks: set[asyncio.Task[None]] = set()


def _schedule_analysis(incident_id: str) -> None:
    """Run the agent in the background, keeping a reference to the task."""
    task = asyncio.create_task(_run_analysis(incident_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_analysis(incident_id: str) -> None:
    """Investigate one incident, publishing progress events as it goes."""
    incident = store.get(incident_id)
    if incident is None:
        logger.warning("analysis requested for unknown incident %s", incident_id)
        return

    store.set_status(incident_id, IncidentStatus.INVESTIGATING, reason="agent analysis started")
    orchestrator = get_orchestrator()

    async def progress(event: dict[str, Any]) -> None:
        store.publish(
            incident_id,
            f"agent.{event.get('phase', 'thought')}",
            message=str(event.get("message") or ""),
            data={"tool": event.get("tool"), "details": event.get("data") or {}},
        )

    try:
        result = await orchestrator.diagnose(
            incident.model_dump(mode="json"), progress=progress
        )
        diagnosis = Diagnosis.model_validate(
            {**result, "requires_human_approval": bool(result.get("commands"))}
        )
    except Exception as exc:
        logger.exception("agent analysis failed for %s", incident_id)
        store.publish(incident_id, "error", message=f"Agent analysis failed: {exc}")
        store.set_status(incident_id, IncidentStatus.OPEN, reason="analysis failed")
        return

    store.publish(
        incident_id,
        "diagnosis.completed",
        message=diagnosis.summary,
        data={"diagnosis": diagnosis.model_dump(mode="json")},
    )
    final_status = (
        IncidentStatus.AWAITING_APPROVAL
        if diagnosis.requires_human_approval and diagnosis.commands
        else IncidentStatus.RESOLVED
    )
    store.set_diagnosis(incident_id, diagnosis, final_status=final_status)
    store.publish(
        incident_id,
        "status",
        message=final_status.value,
        data={"status": final_status.value},
    )


# ---------------------------------------------------------------------------
# Webhook helpers
# ---------------------------------------------------------------------------
def _incident_from_alert(alert: AlertmanagerAlert) -> IncidentCreate:
    labels = dict(alert.labels)
    annotations = dict(alert.annotations)
    alertname = labels.get("alertname") or "Prometheus alert"
    summary = annotations.get("summary") or annotations.get("description") or ""
    description = annotations.get("description") or summary or f"{alertname} fired"

    return IncidentCreate(
        title=f"{alertname} ({alert.status})"[:200],
        description=description[:8000],
        namespace=labels.get("namespace") or "default",
        workload=labels.get("pod") or labels.get("deployment") or labels.get("workload"),
        severity=_severity_from(labels.get("severity")),
        source="prometheus",
        labels={
            **labels,
            "fingerprint": alert.fingerprint or "",
            "alert_status": alert.status,
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=Incident,
    status_code=status.HTTP_201_CREATED,
    summary="Report a new incident",
)
async def create_incident(payload: IncidentCreate) -> Incident:
    return store.create(payload)


@router.post(
    "/webhook",
    response_model=WebhookIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest an Alertmanager or custom webhook alert",
)
async def ingest_webhook(payload: WebhookRequest) -> WebhookIngestResponse:
    candidates: list[IncidentCreate] = []
    skipped = 0

    if payload.alerts:
        for alert in payload.alerts:
            if alert.status.strip().lower() == "resolved":
                skipped += 1
                continue
            candidate = _incident_from_alert(alert)
            fingerprint = candidate.labels.get("fingerprint") or ""
            if store.find_open_by_fingerprint(fingerprint):
                skipped += 1
                continue
            candidates.append(candidate)
        message = (
            f"Ingested {len(candidates)} firing alert(s) from Prometheus."
            if candidates
            else "No firing alerts to ingest (resolved or already tracked)."
        )
    else:
        candidates.append(
            IncidentCreate(
                title=(payload.title or "Custom alert")[:200],
                description=(payload.description or payload.title or "Custom alert")[:8000],
                namespace=payload.namespace or "default",
                workload=payload.workload,
                severity=payload.severity or _severity_from(payload.labels.get("severity")),
                source=payload.source or "webhook",
                labels=dict(payload.labels),
            )
        )
        message = "Ingested 1 custom alert."

    created = [store.create(candidate) for candidate in candidates]
    return WebhookIngestResponse(
        created=created,
        created_count=len(created),
        skipped=skipped,
        message=message,
    )


@router.post(
    "/simulate",
    response_model=SimulateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger a chaos scenario (OOMKilled, CPU Spike, Config Error)",
)
async def simulate_incident(payload: SimulateRequest) -> SimulateResponse:
    orchestrator = get_orchestrator()
    try:
        alert = orchestrator.simulator.trigger_mock_incident(payload.scenario)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    incident = store.create(
        IncidentCreate(
            title=str(alert["title"])[:200],
            description=str(alert["description"])[:8000],
            namespace=str(alert.get("namespace") or "default"),
            workload=alert.get("pod"),
            severity=_severity_from(alert.get("severity")),
            source="chaos-simulator",
            labels={
                "incident_type": str(alert.get("incident_type")),
                "alert_id": str(alert.get("alert_id")),
                "node": str(alert.get("node") or ""),
            },
        )
    )

    analysis_started = False
    if payload.auto_analyze:
        _schedule_analysis(incident.id)
        analysis_started = True

    return SimulateResponse(
        incident=incident, scenario=alert, analysis_started=analysis_started
    )


@router.get("", response_model=list[Incident], summary="List recent incidents")
async def list_incidents(
    status_filter: IncidentStatus | None = Query(
        default=None,
        alias="status",
        description="Filter by lifecycle status.",
    ),
    limit: int = Query(default=50, ge=1, le=MAX_INCIDENTS),
) -> list[Incident]:
    return store.list(status_filter=status_filter, limit=limit)


@router.get("/{incident_id}", response_model=Incident, summary="Fetch one incident")
async def get_incident(incident_id: str) -> Incident:
    return _require(incident_id)


@router.post(
    "/{incident_id}/analyze",
    response_model=Incident,
    summary="Run the agent against an incident (blocking)",
)
async def analyze_incident(incident_id: str) -> Incident:
    _require(incident_id)
    await _run_analysis(incident_id)
    return _require(incident_id)


@router.post(
    "/{incident_id}/approve",
    response_model=Incident,
    summary="Approve the pending remediation (human-in-the-loop)",
)
async def approve_incident(incident_id: str, payload: ApproveRequest) -> Incident:
    incident = _require(incident_id)
    if incident.status is not IncidentStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Incident '{incident_id}' is {incident.status.value}; "
                "no remediation is awaiting approval"
            ),
        )

    approval = ApprovalRecord(
        approved_by=payload.approved_by,
        notes=payload.notes,
        approved_actions=list(incident.diagnosis.commands) if incident.diagnosis else [],
    )
    store.publish(
        incident_id,
        "approval.granted",
        message=f"Remediation approved by {payload.approved_by}",
        data={"approved_by": payload.approved_by, "actions": approval.approved_actions},
    )
    resolved = store.set_approval(incident_id, approval)
    store.publish(
        incident_id,
        "status",
        message=IncidentStatus.RESOLVED.value,
        data={"status": IncidentStatus.RESOLVED.value},
    )
    return resolved


@router.post(
    "/{incident_id}/resolve",
    response_model=Incident,
    summary="Mark an incident as resolved",
)
async def resolve_incident(incident_id: str) -> Incident:
    _require(incident_id)
    return store.set_status(incident_id, IncidentStatus.RESOLVED, reason="manually resolved")
