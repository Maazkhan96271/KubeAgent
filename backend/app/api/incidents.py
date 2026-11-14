"""Incident intake, retrieval and agent-driven analysis endpoints."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.agent.orchestrator import AgentOrchestrator
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/incidents", tags=["incidents"])


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class IncidentStatus(str, Enum):
    open = "open"
    analyzing = "analyzing"
    diagnosed = "diagnosed"
    failed = "failed"
    resolved = "resolved"


class IncidentCreate(BaseModel):
    """Payload accepted when an incident is reported."""

    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=3, max_length=8000)
    namespace: str = Field(default="default", max_length=253)
    workload: str | None = Field(
        default=None, max_length=253, description="Deployment or pod name, when known."
    )
    severity: Severity = Severity.medium
    source: str = Field(default="manual", max_length=64)
    labels: dict[str, str] = Field(default_factory=dict)


class Diagnosis(BaseModel):
    """Structured agent output for one incident."""

    summary: str
    root_cause: str
    severity: Severity
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    generated_by: str = "rules:builtin"
    created_at: datetime = Field(default_factory=utcnow)


class Incident(IncidentCreate):
    """A stored incident record."""

    id: str
    status: IncidentStatus = IncidentStatus.open
    created_at: datetime
    updated_at: datetime
    diagnosis: Diagnosis | None = None


class IncidentStore:
    """In-memory incident registry with best-effort pub/sub for SSE clients.

    Swap this class for a database-backed repository when persistence is needed;
    the router only depends on its async method surface.
    """

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    # -- queries -------------------------------------------------------
    async def get(self, incident_id: str) -> Incident | None:
        return self._incidents.get(incident_id)

    async def list(
        self,
        status_filter: IncidentStatus | None = None,
        limit: int = 50,
    ) -> list[Incident]:
        items = sorted(
            self._incidents.values(), key=lambda item: item.created_at, reverse=True
        )
        if status_filter is not None:
            items = [item for item in items if item.status is status_filter]
        return items[: max(limit, 1)]

    # -- mutations -----------------------------------------------------
    async def create(self, payload: IncidentCreate) -> Incident:
        now = utcnow()
        incident = Incident(
            id=f"inc-{uuid.uuid4().hex[:12]}",
            status=IncidentStatus.open,
            created_at=now,
            updated_at=now,
            **payload.model_dump(),
        )
        async with self._lock:
            self._incidents[incident.id] = incident
        self.publish(
            {"type": "incident.created", "incident": incident.model_dump(mode="json")}
        )
        return incident

    async def set_status(self, incident_id: str, new_status: IncidentStatus) -> Incident:
        return await self._patch(incident_id, status=new_status)

    async def set_diagnosis(self, incident_id: str, diagnosis: Diagnosis) -> Incident:
        return await self._patch(
            incident_id, diagnosis=diagnosis, status=IncidentStatus.diagnosed
        )

    async def _patch(self, incident_id: str, **changes: Any) -> Incident:
        async with self._lock:
            incident = self._incidents.get(incident_id)
            if incident is None:
                raise KeyError(incident_id)
            updated = incident.model_copy(
                update={**changes, "updated_at": utcnow()}
            )
            self._incidents[incident_id] = updated
        self.publish(
            {"type": "incident.updated", "incident": updated.model_dump(mode="json")}
        )
        return updated

    # -- pub/sub -------------------------------------------------------
    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("Dropping event for a slow SSE subscriber")

    @asynccontextmanager
    async def subscription(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


#: Process-wide store; replaced by a repository in a persistent deployment.
store = IncidentStore()


@lru_cache(maxsize=1)
def get_orchestrator() -> AgentOrchestrator:
    """FastAPI dependency returning a shared orchestrator instance."""
    return AgentOrchestrator(get_settings())


async def _require(incident_id: str) -> Incident:
    incident = await store.get(incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found",
        )
    return incident


@router.post(
    "",
    response_model=Incident,
    status_code=status.HTTP_201_CREATED,
    summary="Report a new incident",
)
async def create_incident(payload: IncidentCreate) -> Incident:
    return await store.create(payload)


@router.get("", response_model=list[Incident], summary="List incidents")
async def list_incidents(
    status_filter: IncidentStatus | None = Query(
        default=None, alias="status", description="Filter by incident status."
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Incident]:
    return await store.list(status_filter=status_filter, limit=limit)


@router.get("/{incident_id}", response_model=Incident, summary="Fetch one incident")
async def get_incident(incident_id: str) -> Incident:
    return await _require(incident_id)


@router.post(
    "/{incident_id}/analyze",
    response_model=Incident,
    summary="Run the agent against an incident",
)
async def analyze_incident(
    incident_id: str,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
) -> Incident:
    incident = await _require(incident_id)
    await store.set_status(incident.id, IncidentStatus.analyzing)

    try:
        result = await orchestrator.diagnose(incident.model_dump(mode="json"))
    except Exception as exc:
        logger.exception("Agent analysis failed for %s", incident.id)
        await store.set_status(incident.id, IncidentStatus.failed)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Agent analysis failed: {exc}",
        ) from exc

    try:
        diagnosis = Diagnosis.model_validate(result)
    except Exception as exc:
        logger.exception("Agent returned an invalid diagnosis for %s", incident.id)
        await store.set_status(incident.id, IncidentStatus.failed)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Agent returned an invalid diagnosis: {exc}",
        ) from exc

    return await store.set_diagnosis(incident.id, diagnosis)


@router.post(
    "/{incident_id}/resolve",
    response_model=Incident,
    summary="Mark an incident as resolved",
)
async def resolve_incident(incident_id: str) -> Incident:
    await _require(incident_id)
    return await store.set_status(incident_id, IncidentStatus.resolved)
