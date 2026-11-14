"""Cluster topology for the interactive map, plus per-pod crash simulation.

The topology is a declarative model of the demo cluster (one control plane and
three workers). Pod health is derived from the live incident store, so a pod
turns red as soon as a real incident names it, and ``crash`` creates one.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api import incidents as incidents_api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/topology", tags=["topology"])

MASTER = {"name": "control-plane-us-east-1", "zone": "us-east-1a", "status": "Ready"}

NODES: list[dict[str, Any]] = [
    {"name": "node-us-east-1a", "zone": "us-east-1a", "status": "Ready", "memory_percent": 68},
    {"name": "node-us-east-1b", "zone": "us-east-1b", "status": "Ready", "memory_percent": 74},
    {"name": "node-us-east-1c", "zone": "us-east-1c", "status": "Degraded", "memory_percent": 92},
]

#: Declarative workload layout. ``memory`` values are the container cgroup limits.
PODS: list[dict[str, Any]] = [
    {
        "name": "payment-service",
        "namespace": "default",
        "node": "node-us-east-1c",
        "role": "payments",
        "ip": "10.244.2.17",
        "restarts": 4,
        "last_restart": "5m ago",
        "memory": {"limit_mb": 512, "used_mb": 498},
    },
    {
        "name": "auth-service",
        "namespace": "default",
        "node": "node-us-east-1a",
        "role": "identity",
        "ip": "10.244.1.9",
        "restarts": 0,
        "last_restart": None,
        "memory": {"limit_mb": 256, "used_mb": 131},
    },
    {
        "name": "redis-cache",
        "namespace": "default",
        "node": "node-us-east-1a",
        "role": "cache",
        "ip": "10.244.1.22",
        "restarts": 0,
        "last_restart": None,
        "memory": {"limit_mb": 256, "used_mb": 118},
    },
    {
        "name": "checkout-worker",
        "namespace": "default",
        "node": "node-us-east-1b",
        "role": "queue consumer",
        "ip": "10.244.3.41",
        "restarts": 1,
        "last_restart": "2h ago",
        "memory": {"limit_mb": 512, "used_mb": 233},
    },
]

#: Inter-service traffic drawn as animated edges.
LINKS: list[dict[str, str]] = [
    {"from": "payment-service", "to": "redis-cache"},
    {"from": "auth-service", "to": "redis-cache"},
    {"from": "checkout-worker", "to": "payment-service"},
]

_POD_BY_NAME = {pod["name"]: pod for pod in PODS}


def _latest_incident(pod_name: str) -> incidents_api.Incident | None:
    for incident in incidents_api.store.list(limit=incidents_api.MAX_INCIDENTS):
        workload = str(incident.workload or "").lower()
        if workload == pod_name or pod_name in str(incident.title or "").lower():
            return incident
    return None


def _pod_state(pod: dict[str, Any]) -> dict[str, Any]:
    incident = _latest_incident(pod["name"])
    if incident is not None and incident.status is not incidents_api.IncidentStatus.RESOLVED:
        state = "critical" if incident.severity.value in {"critical", "high"} else "degraded"
    elif pod["name"] == "checkout-worker":
        state = "active"
    else:
        state = "healthy"

    memory = pod["memory"]
    percent = round(memory["used_mb"] / memory["limit_mb"] * 100, 1)
    return {
        "name": pod["name"],
        "namespace": pod["namespace"],
        "node": pod["node"],
        "role": pod["role"],
        "ip": pod["ip"],
        "restarts": pod["restarts"],
        "last_restart": pod["last_restart"],
        "status": state,
        "incident_id": incident.id if incident else None,
        "incident_status": incident.status.value if incident else None,
        "memory": {**memory, "percent": percent},
    }


@router.get("", summary="Cluster topology with live pod states")
async def get_topology() -> dict[str, Any]:
    pods = [_pod_state(pod) for pod in PODS]
    return {
        "cluster": "vke-prod-us-east-1",
        "master": MASTER,
        "nodes": [
            {
                **node,
                "pods": [pod for pod in pods if pod["node"] == node["name"]],
            }
            for node in NODES
        ],
        "links": LINKS,
        "counts": {
            "nodes": len(NODES),
            "ready_nodes": len([node for node in NODES if node["status"] == "Ready"]),
            "pods": len(pods),
            "critical_pods": len([pod for pod in pods if pod["status"] == "critical"]),
        },
    }


@router.post(
    "/pods/{pod_name}/crash",
    status_code=status.HTTP_201_CREATED,
    summary="Trigger an incident for a single pod from the topology view",
)
async def crash_pod(pod_name: str) -> dict[str, Any]:
    pod = _POD_BY_NAME.get(pod_name)
    if pod is None:
        raise HTTPException(status_code=404, detail=f"unknown pod '{pod_name}'")

    memory = pod["memory"]
    incident = incidents_api.store.create(
        incidents_api.IncidentCreate(
            title=f"{pod_name} crashed (topology-triggered)",
            description=(
                f"Operator triggered a crash for {pod_name} from the cluster topology. "
                f"Container memory is at {memory['used_mb']}MiB of its {memory['limit_mb']}MiB "
                "cgroup limit; investigate the restart loop."
            ),
            namespace=pod["namespace"],
            workload=pod_name,
            severity=incidents_api.Severity.critical,
            source="topology",
            labels={"pod": pod_name, "node": pod["node"]},
        )
    )
    incidents_api._schedule_analysis(incident.id)
    logger.info("topology crash triggered for %s → %s", pod_name, incident.id)
    return {"incident": incident, "pod": _pod_state(pod)}
