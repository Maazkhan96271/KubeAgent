"""Operator-facing system endpoints: safety policy, Prometheus metrics, SRE CLI."""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.api import incidents as incidents_api
from app.config import get_settings
from app.policy import PolicyState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])
metrics_router = APIRouter(tags=["system"], include_in_schema=False)


@lru_cache(maxsize=1)
def get_policy() -> PolicyState:
    """Process-wide policy state initialised from settings."""
    settings = get_settings()
    return PolicyState(
        read_only=settings.read_only_mode,
        protected_namespaces=settings.policy_protected_namespace_list,
        max_approvals_per_window=settings.policy_max_approvals_per_window,
        window_seconds=settings.policy_window_seconds,
    )


# ---------------------------------------------------------------------------
# Safety policy
# ---------------------------------------------------------------------------
class PolicyUpdate(BaseModel):
    read_only: bool | None = None
    protected_namespaces: list[str] | None = None
    max_approvals_per_window: int | None = Field(default=None, ge=1, le=50)


@router.get("/policy", summary="Current SRE safety policy")
async def read_policy() -> dict[str, Any]:
    return get_policy().snapshot()


@router.patch("/policy", summary="Update the safety policy (read-only, protected namespaces, rate limit)")
async def update_policy(payload: PolicyUpdate) -> dict[str, Any]:
    return get_policy().update(
        read_only=payload.read_only,
        protected_namespaces=payload.protected_namespaces,
        max_approvals_per_window=payload.max_approvals_per_window,
    )


# ---------------------------------------------------------------------------
# Prometheus exposition (no extra dependency: hand-rolled text format)
# ---------------------------------------------------------------------------
def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@metrics_router.get("/metrics", response_class=PlainTextResponse, summary="Prometheus metrics")
async def metrics() -> PlainTextResponse:
    store = incidents_api.store
    policy = get_policy().snapshot()
    all_incidents = store.list(limit=incidents_api.MAX_INCIDENTS)
    active = [item for item in all_incidents if item.status is not incidents_api.IncidentStatus.RESOLVED]
    resolved = [item for item in all_incidents if item.status is incidents_api.IncidentStatus.RESOLVED]

    durations = []
    for incident in resolved:
        seconds = (incident.updated_at - incident.created_at).total_seconds()
        if seconds >= 0:
            durations.append(seconds)

    buckets = [1, 5, 15, 30, 60, 120, 300]
    lines: list[str] = [
        "# HELP kubeagent_active_incidents Incidents that are not resolved yet.",
        "# TYPE kubeagent_active_incidents gauge",
        f"kubeagent_active_incidents {len(active)}",
        "",
        "# HELP kubeagent_incidents_total Incidents by lifecycle status.",
        "# TYPE kubeagent_incidents_total gauge",
    ]
    for status in incidents_api.IncidentStatus:
        count = len([item for item in all_incidents if item.status is status])
        lines.append(f'kubeagent_incidents_total{{status="{_escape_label(status.value)}"}} {count}')
    lines += [
        "",
        "# HELP kubeagent_remediations_total Remediation approvals by outcome.",
        "# TYPE kubeagent_remediations_total counter",
        f'kubeagent_remediations_total{{status="success"}} {policy["remediations_success"]}',
        f'kubeagent_remediations_total{{status="failed"}} {policy["remediations_failed"]}',
        "",
        "# HELP kubeagent_mttr_seconds Mean time to resolve, in seconds.",
        "# TYPE kubeagent_mttr_seconds histogram",
    ]
    for bucket in buckets:
        count = len([value for value in durations if value <= bucket])
        lines.append(f'kubeagent_mttr_seconds_bucket{{le="{bucket}"}} {count}')
    lines.append(f'kubeagent_mttr_seconds_bucket{{le="+Inf"}} {len(durations)}')
    lines.append(f"kubeagent_mttr_seconds_sum {round(sum(durations), 3)}")
    lines.append(f"kubeagent_mttr_seconds_count {len(durations)}")
    lines += [
        "",
        "# HELP kubeagent_read_only_mode Whether remediation approval is blocked entirely.",
        "# TYPE kubeagent_read_only_mode gauge",
        f"kubeagent_read_only_mode {1 if policy['read_only'] else 0}",
        "",
    ]
    return PlainTextResponse("\n".join(lines), media_type="text/plain; version=0.0.4")


# ---------------------------------------------------------------------------
# Read-only SRE CLI
# ---------------------------------------------------------------------------
class CliRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=300)


class CliResponse(BaseModel):
    ok: bool
    output: str


RUNBOOKS: dict[str, str] = {
    "OOMKilled": "Check the container limit and heap settings, raise the limit or fix the leak, then roll the deployment.",
    "CrashLoopBackOff": "Read previous logs, verify required secrets and env vars, then restart once corrected.",
    "ZombieProcessLeak": "Reap children (SIGCHLD/wait), restart the leaking workload, verify fork() succeeds.",
    "CPUSaturation": "Compare requests/limits with node capacity, scale out or raise the limit, re-check throttling.",
    "ConfigError": "Confirm the referenced secret keys exist, patch the missing key, delete the failing pod.",
    "ImagePull": "Verify the image tag exists and the imagePullSecret is valid.",
}


def _help_text() -> str:
    return "\n".join(
        [
            "kubeagent status                     cluster, incident and policy summary",
            "kubeagent inspect pod/<name>         describe one unhealthy pod",
            "kubeagent runbooks list              built-in remediation runbooks",
            "kubeagent policy                     active safety policy",
            "help                                 this message",
        ]
    )


async def _cli_status() -> str:
    orchestrator = incidents_api.get_orchestrator()
    settings = get_settings()
    policy = get_policy().snapshot()
    lines = ["CLUSTER SUMMARY"]
    for namespace in settings.namespace_list:
        try:
            report = await asyncio.to_thread(orchestrator.k8s.get_unhealthy_pods, namespace)
        except Exception as exc:  # collector failures are reported, not raised
            lines.append(f"  {namespace}: collector error ({exc})")
            continue
        unhealthy = len(report.issues)
        lines.append(
            f"  {namespace}: {report.total_pods} pods, {unhealthy} unhealthy"
        )
    incidents = incidents_api.store.list(limit=incidents_api.MAX_INCIDENTS)
    active = len([item for item in incidents if item.status is not incidents_api.IncidentStatus.RESOLVED])
    lines += [
        f"  incidents: {active} active / {len(incidents)} tracked",
        f"  simulation_mode: {settings.simulation_mode}",
        f"  reasoning: {'llm:' + settings.llm_model if settings.llm_enabled else 'heuristic'}",
        f"  read_only: {policy['read_only']}  protected: {', '.join(policy['protected_namespaces']) or 'none'}",
    ]
    return "\n".join(lines)


async def _cli_inspect(target: str) -> str:
    pod_name = target.split("/", 1)[1] if "/" in target else target
    if not pod_name:
        return "usage: kubeagent inspect pod/<name>"
    orchestrator = incidents_api.get_orchestrator()
    settings = get_settings()
    for namespace in settings.namespace_list:
        try:
            report = await asyncio.to_thread(orchestrator.k8s.get_unhealthy_pods, namespace)
        except Exception:
            continue
        for issue in report.issues:
            if issue.name == pod_name:
                return "\n".join(
                    [
                        f"pod: {issue.name}",
                        f"namespace: {namespace}",
                        f"node: {issue.node}",
                        f"phase: {issue.phase}  ready: {issue.ready}",
                        f"reason: {issue.reason}  restarts: {issue.restarts}  exit_code: {issue.exit_code}",
                        f"image: {issue.image}",
                    ]
                )
    for incident in incidents_api.store.list(limit=incidents_api.MAX_INCIDENTS):
        for line in (incident.diagnosis.evidence if incident.diagnosis else []) or []:
            if pod_name in line:
                return f"pod: {pod_name}\n(not in the current unhealthy set)\nevidence: {line}"
    return f"pod '{pod_name}' not found in the watched namespaces."


async def _run_cli(raw: str) -> tuple[bool, str]:
    tokens = raw.strip().split()
    if tokens and tokens[0].lower() in {"kubeagent", "ka"}:
        tokens = tokens[1:]
    if not tokens or tokens[0].lower() in {"help", "?"}:
        return True, _help_text()
    verb = tokens[0].lower()
    if verb in {"status", "health"}:
        return True, await _cli_status()
    if verb == "policy":
        snapshot = get_policy().snapshot()
        return True, "\n".join(f"{key}: {value}" for key, value in snapshot.items())
    if verb == "inspect":
        return True, await _cli_inspect(tokens[1] if len(tokens) > 1 else "")
    if verb in {"runbooks", "runbook"} and len(tokens) > 1 and tokens[1].lower() == "list":
        return True, "\n".join(f"{name:<20} {text}" for name, text in RUNBOOKS.items())
    return False, f"unknown command: {' '.join(tokens)}\n\n{_help_text()}"


@router.post("/cli", response_model=CliResponse, summary="Run an allowlisted read-only SRE CLI command")
async def run_cli(payload: CliRequest) -> CliResponse:
    ok, output = await _run_cli(payload.command)
    return CliResponse(ok=ok, output=output)
