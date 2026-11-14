"""Deterministic fake cluster used when ``SIMULATION_MODE`` is enabled.

The scenario is stable across restarts so demos, screenshots and tests always
see the same incident: a payment worker OOM-killing on a memory-pressured node.
"""

from __future__ import annotations

from typing import Any

_CRASHING_POD = "payment-worker-7d9c4b6f5-x2m8q"

_PODS: list[dict[str, Any]] = [
    {
        "name": "checkout-api-6f8b9c7d4-4kq2p",
        "namespace": "default",
        "phase": "Running",
        "reason": None,
        "last_state": None,
        "ready": True,
        "restarts": 0,
        "node": "worker-1",
        "image": "ghcr.io/acme/checkout-api:1.14.2",
        "age_seconds": 9180,
    },
    {
        "name": "checkout-api-6f8b9c7d4-h7t9w",
        "namespace": "default",
        "phase": "Running",
        "reason": None,
        "last_state": None,
        "ready": True,
        "restarts": 0,
        "node": "worker-1",
        "image": "ghcr.io/acme/checkout-api:1.14.2",
        "age_seconds": 9180,
    },
    {
        "name": _CRASHING_POD,
        "namespace": "default",
        "phase": "Running",
        "reason": "CrashLoopBackOff",
        "last_state": "OOMKilled",
        "ready": False,
        "restarts": 7,
        "node": "worker-2",
        "image": "ghcr.io/acme/payment-worker:2.3.0",
        "age_seconds": 5400,
    },
    {
        "name": "redis-cache-0",
        "namespace": "default",
        "phase": "Running",
        "reason": None,
        "last_state": None,
        "ready": True,
        "restarts": 1,
        "node": "worker-2",
        "image": "redis:7.2-alpine",
        "age_seconds": 864000,
    },
    {
        "name": "reporting-cron-28934112-p8vzn",
        "namespace": "default",
        "phase": "Pending",
        "reason": None,
        "last_state": None,
        "ready": False,
        "restarts": 0,
        "node": None,
        "image": "ghcr.io/acme/reporting:0.9.1",
        "age_seconds": 240,
    },
    {
        "name": "coredns-5dd5756b68-9wz7k",
        "namespace": "kube-system",
        "phase": "Running",
        "reason": None,
        "last_state": None,
        "ready": True,
        "restarts": 0,
        "node": "control-plane",
        "image": "registry.k8s.io/coredns/coredns:v1.11.1",
        "age_seconds": 864000,
    },
    {
        "name": "kube-proxy-x4lmn",
        "namespace": "kube-system",
        "phase": "Running",
        "reason": None,
        "last_state": None,
        "ready": True,
        "restarts": 0,
        "node": "worker-2",
        "image": "registry.k8s.io/kube-proxy:v1.30.2",
        "age_seconds": 864000,
    },
]

_EVENTS: list[dict[str, Any]] = [
    {
        "type": "Warning",
        "reason": "OOMKilling",
        "message": (
            "Memory cgroup out of memory: killed process 1 (payment-worker) "
            "total-vm:2148736kB, anon-rss:1042432kB, limit:1073741824"
        ),
        "object": f"pod/{_CRASHING_POD}",
        "count": 5,
        "last_timestamp": "2026-11-14T09:41:12Z",
    },
    {
        "type": "Warning",
        "reason": "BackOff",
        "message": "Back-off restarting failed container payment-worker in pod payment-worker-7d9c4b6f5-x2m8q",
        "object": f"pod/{_CRASHING_POD}",
        "count": 12,
        "last_timestamp": "2026-11-14T09:44:03Z",
    },
    {
        "type": "Warning",
        "reason": "FailedScheduling",
        "message": "0/3 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane}, 2 Insufficient memory.",
        "object": "pod/reporting-cron-28934112-p8vzn",
        "count": 3,
        "last_timestamp": "2026-11-14T09:40:28Z",
    },
    {
        "type": "Warning",
        "reason": "EvictionThresholdMet",
        "message": "Attempting to reclaim memory on node worker-2",
        "object": "node/worker-2",
        "count": 2,
        "last_timestamp": "2026-11-14T09:38:51Z",
    },
    {
        "type": "Normal",
        "reason": "Pulled",
        "message": "Successfully pulled image ghcr.io/acme/checkout-api:1.14.2",
        "object": "pod/checkout-api-6f8b9c7d4-4kq2p",
        "count": 1,
        "last_timestamp": "2026-11-14T09:20:04Z",
    },
]

_NODES: list[dict[str, Any]] = [
    {
        "name": "control-plane",
        "ready": True,
        "roles": ["control-plane"],
        "conditions": [
            {"type": "Ready", "status": "True"},
            {"type": "MemoryPressure", "status": "False"},
            {"type": "DiskPressure", "status": "False"},
        ],
        "capacity": {"cpu": "4", "memory": "8Gi", "pods": "110"},
    },
    {
        "name": "worker-1",
        "ready": True,
        "roles": ["worker"],
        "conditions": [
            {"type": "Ready", "status": "True"},
            {"type": "MemoryPressure", "status": "False"},
            {"type": "DiskPressure", "status": "False"},
        ],
        "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
    },
    {
        "name": "worker-2",
        "ready": True,
        "roles": ["worker"],
        "conditions": [
            {"type": "Ready", "status": "True"},
            {"type": "MemoryPressure", "status": "True"},
            {"type": "DiskPressure", "status": "False"},
        ],
        "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
    },
]

_LOGS: dict[str, str] = {
    _CRASHING_POD: "\n".join(
        [
            "2026-11-14T09:40:51Z INFO  worker starting (pid=1, workers=4)",
            "2026-11-14T09:40:52Z INFO  connected to redis-cache:6379",
            "2026-11-14T09:40:53Z INFO  consuming queue=payments.retry",
            "2026-11-14T09:41:02Z INFO  batch size=5000 batch_id=b-88121",
            "2026-11-14T09:41:07Z WARN  batch memory footprint 1.8GiB exceeds batch budget 512MiB",
            "2026-11-14T09:41:11Z WARN  gc pause 812ms, heap 1.9GiB",
            "2026-11-14T09:41:12Z FATAL runtime: out of memory: cannot allocate 268435456-byte block",
            "2026-11-14T09:41:12Z FATAL signal: killed (OOMKilled)",
        ]
    )
}

_DEFAULT_LOGS = "\n".join(
    [
        "2026-11-14T09:40:00Z INFO  service started",
        "2026-11-14T09:41:00Z INFO  healthcheck ok",
    ]
)


class SimulatedCluster:
    """Produces a realistic, stable incident scenario for demos and tests."""

    def __init__(self, seed: int = 1337) -> None:
        # ``seed`` is accepted for API symmetry with future randomised scenarios.
        self.seed = seed

    def pods(self, namespace: str = "default") -> list[dict[str, Any]]:
        return [dict(pod) for pod in _PODS if pod["namespace"] == namespace]

    def events(self, namespace: str = "default") -> list[dict[str, Any]]:
        if namespace not in {"default", "kube-system"}:
            return []
        return [dict(event) for event in _EVENTS]

    def nodes(self) -> list[dict[str, Any]]:
        return [dict(node) for node in _NODES]

    def logs(self, pod_name: str, tail_lines: int = 100) -> str:
        body = _LOGS.get(pod_name, _DEFAULT_LOGS)
        return "\n".join(body.splitlines()[-max(tail_lines, 1) :])
