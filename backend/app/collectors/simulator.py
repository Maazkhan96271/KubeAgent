"""Deterministic Chaos Incident Simulator backing ``SIMULATION_MODE``.

The engine holds three realistic incident templates and can switch the fake
cluster between them at runtime, so demos and tests never need a live cluster::

    from app.collectors.simulator import SimulatedCluster

    cluster = SimulatedCluster()
    alert = cluster.trigger_mock_incident("Zombie_Process_Leak")

``trigger_mock_incident`` activates a template and returns the alert payload an
ingest pipeline would normally receive.  The collector-facing read methods
(``pods``, ``events``, ``nodes``, ``logs``, ``host_signals``) then only expose
the active scenario, which is what makes the demo 100% reproducible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

DEFAULT_INCIDENT = "OOMKilled_Payment_Service"


class MockIncidentType(str, Enum):
    """Supported chaos scenarios."""

    OOM_KILLED_PAYMENT_SERVICE = "OOMKilled_Payment_Service"
    ZOMBIE_PROCESS_LEAK = "Zombie_Process_Leak"
    CRASHLOOP_DATABASE_CONFIG = "CrashLoopBackOff_Database_Config"
    CPU_SPIKE_WORKER_SATURATION = "CPU_Spike_Worker_Saturation"


# ---------------------------------------------------------------------------
# Template definition
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IncidentTemplate:
    """Everything the simulator needs to serve one scenario."""

    incident_type: MockIncidentType
    title: str
    description: str
    severity: str
    namespace: str
    workload: str
    pod_name: str
    node: str
    signals: list[str]
    host_signals: dict[str, Any]
    pods: list[dict[str, Any]]
    events: list[dict[str, Any]]
    logs: str
    node_pressures: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Baseline (healthy) cluster
# ---------------------------------------------------------------------------
def _container(
    name: str,
    *,
    ready: bool = True,
    restarts: int = 0,
    reason: str | None = None,
    exit_code: int | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "ready": ready,
        "restarts": restarts,
        "reason": reason,
        "exit_code": exit_code,
        "message": message,
    }


def _pod(
    name: str,
    *,
    namespace: str = "default",
    node: str = "worker-1",
    phase: str = "Running",
    ready: bool = True,
    restarts: int = 0,
    reason: str | None = None,
    last_state: str | None = None,
    exit_code: int | None = None,
    image: str | None = None,
    containers: list[dict[str, Any]] | None = None,
    message: str | None = None,
    age_seconds: int = 5400,
) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": namespace,
        "node": node,
        "phase": phase,
        "ready": ready,
        "restarts": restarts,
        "reason": reason,
        "last_state": last_state,
        "exit_code": exit_code,
        "image": image,
        "age_seconds": age_seconds,
        "containers": containers
        if containers is not None
        else [_container(name.rsplit("-", 2)[0], ready=ready, restarts=restarts)],
        "message": message,
    }


_BASELINE_PODS: list[dict[str, Any]] = [
    _pod(
        "checkout-api-6f8b9c7d4-4kq2p",
        image="ghcr.io/acme/checkout-api:1.14.2",
        age_seconds=9180,
    ),
    _pod(
        "checkout-api-6f8b9c7d4-h7t9w",
        image="ghcr.io/acme/checkout-api:1.14.2",
        age_seconds=9180,
    ),
    _pod(
        "redis-cache-0",
        node="worker-2",
        image="redis:7.2-alpine",
        restarts=1,
        age_seconds=864000,
    ),
    _pod(
        "coredns-5dd5756b68-9wz7k",
        namespace="kube-system",
        node="control-plane",
        image="registry.k8s.io/coredns/coredns:v1.11.1",
        age_seconds=864000,
    ),
    _pod(
        "kube-proxy-x4lmn",
        namespace="kube-system",
        node="worker-2",
        image="registry.k8s.io/kube-proxy:v1.30.2",
        age_seconds=864000,
    ),
]

_BASELINE_NODES: list[dict[str, Any]] = [
    {
        "name": "control-plane",
        "ready": True,
        "roles": ["control-plane"],
        "conditions": [
            {"type": "Ready", "status": "True", "reason": "KubeletReady", "message": ""},
            {"type": "MemoryPressure", "status": "False", "reason": "", "message": ""},
            {"type": "DiskPressure", "status": "False", "reason": "", "message": ""},
            {"type": "PIDPressure", "status": "False", "reason": "", "message": ""},
        ],
        "capacity": {"cpu": "4", "memory": "8Gi", "pods": "110"},
        "allocatable": {"cpu": "3800m", "memory": "7Gi", "pods": "110"},
        "kubernetes_version": "v1.30.2",
        "age_seconds": 864000,
    },
    {
        "name": "worker-1",
        "ready": True,
        "roles": ["worker"],
        "conditions": [
            {"type": "Ready", "status": "True", "reason": "KubeletReady", "message": ""},
            {"type": "MemoryPressure", "status": "False", "reason": "", "message": ""},
            {"type": "DiskPressure", "status": "False", "reason": "", "message": ""},
            {"type": "PIDPressure", "status": "False", "reason": "", "message": ""},
        ],
        "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
        "allocatable": {"cpu": "7800m", "memory": "15Gi", "pods": "110"},
        "kubernetes_version": "v1.30.2",
        "age_seconds": 864000,
    },
    {
        "name": "worker-2",
        "ready": True,
        "roles": ["worker"],
        "conditions": [
            {"type": "Ready", "status": "True", "reason": "KubeletReady", "message": ""},
            {"type": "MemoryPressure", "status": "False", "reason": "", "message": ""},
            {"type": "DiskPressure", "status": "False", "reason": "", "message": ""},
            {"type": "PIDPressure", "status": "False", "reason": "", "message": ""},
        ],
        "capacity": {"cpu": "8", "memory": "16Gi", "pods": "110"},
        "allocatable": {"cpu": "7800m", "memory": "15Gi", "pods": "110"},
        "kubernetes_version": "v1.30.2",
        "age_seconds": 864000,
    },
]

_BASELINE_LOGS: str = "\n".join(
    [
        "2026-11-14T09:40:00Z INFO  service started (version=1.14.2)",
        "2026-11-14T09:41:00Z INFO  healthcheck ok",
        "2026-11-14T09:42:00Z INFO  request handled in 12ms",
    ]
)

_DEFAULT_LOGS: str = "\n".join(
    [
        "2026-11-14T09:41:00Z INFO  container started",
        "2026-11-14T09:42:00Z INFO  healthcheck ok",
    ]
)


# ---------------------------------------------------------------------------
# Scenario (a): Java payment service OOMKilled at a 512Mi limit
# ---------------------------------------------------------------------------
_PAYMENT_POD = "payment-api-7f9d8c6b5-4xk2p"
_ORDERS_DB_POD = "orders-db-0"
_PYTHON_WORKER_POD = "python-worker-6b48c9f7d-lm4z9"
_CPU_SPIKE_POD = "video-transcoder-5f7c8d9b6-9kq2l"

_OOM_TEMPLATE = IncidentTemplate(
    incident_type=MockIncidentType.OOM_KILLED_PAYMENT_SERVICE,
    title="payment-api is OOMKilled and crash looping",
    description=(
        "The Java payment service exceeds its 512Mi container limit, the kernel "
        "kills it with exit code 137 and the kubelet backs off restarts. Checkout "
        "requests are failing."
    ),
    severity="critical",
    namespace="default",
    workload="payment-api",
    pod_name=_PAYMENT_POD,
    node="worker-2",
    signals=[
        "pod CrashLoopBackOff",
        "last state OOMKilled",
        "container exit code 137",
        "java.lang.OutOfMemoryError: Java heap space",
        "container memory limit 512Mi reached",
    ],
    host_signals={
        "node_memory_percent": 87.4,
        "container_memory_limit_mb": 512,
        "container_memory_usage_mb": 512,
        "container_exit_code": 137,
    },
    pods=[
        _pod(
            _PAYMENT_POD,
            node="worker-2",
            ready=False,
            restarts=6,
            reason="CrashLoopBackOff",
            last_state="OOMKilled",
            exit_code=137,
            image="ghcr.io/acme/payment-api:2.4.1",
            message=(
                "back-off 5m0s restarting failed container=payment-api "
                f"pod={_PAYMENT_POD}_default(3f7c1a2e-9b11-4f0a-8a54-3d9f4b6c2e10)"
            ),
            containers=[
                _container(
                    "payment-api",
                    ready=False,
                    restarts=6,
                    reason="CrashLoopBackOff",
                    exit_code=137,
                    message=(
                        "OOMKilled: container memory limit 512Mi exceeded "
                        "(JVM heap -Xmx512m plus metaspace and thread stacks)"
                    ),
                )
            ],
            age_seconds=5400,
        )
    ],
    events=[
        {
            "type": "Warning",
            "reason": "OOMKilling",
            "message": (
                "Memory cgroup out of memory: killed process 1 (java) "
                "total-vm:1987652kB, anon-rss:524288kB, limit:536870912"
            ),
            "object": f"pod/{_PAYMENT_POD}",
            "count": 6,
            "last_seen": "2026-11-14T09:44:03Z",
        },
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": (
                "Back-off restarting failed container payment-api in pod "
                f"{_PAYMENT_POD}"
            ),
            "object": f"pod/{_PAYMENT_POD}",
            "count": 17,
            "last_seen": "2026-11-14T09:44:12Z",
        },
        {
            "type": "Warning",
            "reason": "Unhealthy",
            "message": "Readiness probe failed: Get http://10.244.1.37:8080/healthz: connection refused",
            "object": f"pod/{_PAYMENT_POD}",
            "count": 9,
            "last_seen": "2026-11-14T09:43:58Z",
        },
    ],
    logs="\n".join(
        [
            "2026-11-14T09:41:02Z INFO  payment-api 2.4.1 starting (java 21.0.4)",
            "2026-11-14T09:41:02Z INFO  JVM args: -Xms256m -Xmx512m -XX:MaxMetaspaceSize=128m",
            "2026-11-14T09:41:05Z INFO  HikariPool-1 - Starting...",
            "2026-11-14T09:41:09Z WARN  GC overhead limit exceeded, heap after GC 498MiB/512MiB",
            "2026-11-14T09:41:11Z ERROR java.lang.OutOfMemoryError: Java heap space",
            "2026-11-14T09:41:11Z ERROR   at com.acme.payment.LedgerIndex.rebuild(LedgerIndex.java:184)",
            "2026-11-14T09:41:11Z ERROR   at com.acme.payment.PaymentConsumer.onMessage(PaymentConsumer.java:96)",
            "2026-11-14T09:41:11Z FATAL container terminated: exit code 137 (SIGKILL / OOMKilled)",
            "2026-11-14T09:41:11Z FATAL memory limit 512Mi exceeded; consider raising the limit or fixing the heap leak",
        ]
    ),
    node_pressures={"worker-2": ["MemoryPressure"]},
)


# ---------------------------------------------------------------------------
# Scenario (b): Python worker leaking zombie children (PID exhaustion)
# ---------------------------------------------------------------------------
_ZOMBIE_TEMPLATE = IncidentTemplate(
    incident_type=MockIncidentType.ZOMBIE_PROCESS_LEAK,
    title="python-worker is leaking child processes (PID exhaustion)",
    description=(
        "The Python worker forks subprocesses without reaping them. Defunct "
        "children accumulate, the PID table fills up, fork() starts returning "
        "EAGAIN and the node shows heavy I/O wait."
    ),
    severity="high",
    namespace="default",
    workload="python-worker",
    pod_name=_PYTHON_WORKER_POD,
    node="worker-2",
    signals=[
        "1,487 defunct (zombie) processes",
        "fork: Resource temporarily unavailable (EAGAIN)",
        "liveness probe failing",
        "CPU iowait 41.8%",
        "PID pressure on worker-2",
    ],
    host_signals={
        "zombie_count": 1487,
        "pid_max": 4194304,
        "pid_usage_percent": 63.2,
        "cpu_iowait_percent": 41.8,
        "load_1m": 18.4,
    },
    pods=[
        _pod(
            _PYTHON_WORKER_POD,
            node="worker-2",
            ready=False,
            restarts=0,
            reason=None,
            image="ghcr.io/acme/python-worker:0.7.3",
            message="Liveness probe failed: fork: retry: Resource temporarily unavailable",
            containers=[
                _container(
                    "python-worker",
                    ready=False,
                    restarts=0,
                    reason=None,
                    message="1,487 defunct children not reaped by the parent process",
                )
            ],
            age_seconds=12600,
        )
    ],
    events=[
        {
            "type": "Warning",
            "reason": "Unhealthy",
            "message": (
                "Liveness probe failed: fork: retry: Resource temporarily unavailable "
                "(EAGAIN), 1487 children in defunct state"
            ),
            "object": f"pod/{_PYTHON_WORKER_POD}",
            "count": 34,
            "last_seen": "2026-11-14T09:45:31Z",
        },
        {
            "type": "Warning",
            "reason": "Failed",
            "message": "container fork failed: Resource temporarily unavailable (EAGAIN)",
            "object": f"pod/{_PYTHON_WORKER_POD}",
            "count": 12,
            "last_seen": "2026-11-14T09:44:57Z",
        },
    ],
    logs="\n".join(
        [
            "2026-11-14T09:38:12Z INFO  worker pool starting (concurrency=8)",
            "2026-11-14T09:39:40Z WARN  spawning child worker pid=48211",
            "2026-11-14T09:40:02Z WARN  spawning child worker pid=48377",
            "2026-11-14T09:40:44Z WARN  child worker pid=48377 exited, parent did not wait()",
            "2026-11-14T09:41:19Z WARN  defunct children=812, reaped=0 (SIGCHLD handler missing)",
            "2026-11-14T09:42:51Z ERROR subprocess fork failed: [Errno 11] Resource temporarily unavailable",
            "2026-11-14T09:43:07Z ERROR OSError: cannot allocate PID, tuning /proc/sys/kernel/pid_max recommended",
            "2026-11-14T09:43:22Z ERROR defunct children=1487, container PID cgroup limit reached",
            "2026-11-14T09:43:30Z FATAL liveness probe failed, container marked unready",
        ]
    ),
    node_pressures={"worker-2": ["PIDPressure"]},
)


# ---------------------------------------------------------------------------
# Scenario (c): database pod crash looping on a missing DB_PASSWORD secret
# ---------------------------------------------------------------------------
_DB_CONFIG_TEMPLATE = IncidentTemplate(
    incident_type=MockIncidentType.CRASHLOOP_DATABASE_CONFIG,
    title="orders-db-0 crash loops on missing DB_PASSWORD",
    description=(
        "The orders database pod starts, fails to read DB_PASSWORD from the "
        "orders-db-credentials secret (the key is absent) and exits with code 1, "
        "so the kubelet keeps restarting it."
    ),
    severity="high",
    namespace="default",
    workload="orders-db",
    pod_name=_ORDERS_DB_POD,
    node="worker-1",
    signals=[
        "pod CrashLoopBackOff",
        "container exit code 1",
        "secret orders-db-credentials missing key DB_PASSWORD",
        "FATAL: environment variable DB_PASSWORD is not set",
    ],
    host_signals={
        "container_exit_code": 1,
        "container_restarts": 11,
        "missing_secret": "orders-db-credentials",
        "missing_key": "DB_PASSWORD",
    },
    pods=[
        _pod(
            _ORDERS_DB_POD,
            node="worker-1",
            ready=False,
            restarts=11,
            reason="CrashLoopBackOff",
            last_state="Error",
            exit_code=1,
            image="postgres:16-alpine",
            message=f"back-off 5m0s restarting failed container=postgres pod={_ORDERS_DB_POD}_default",
            containers=[
                _container(
                    "postgres",
                    ready=False,
                    restarts=11,
                    reason="CrashLoopBackOff",
                    exit_code=1,
                    message="container exited with code 1: DB_PASSWORD is not set",
                )
            ],
            age_seconds=780,
        )
    ],
    events=[
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": (
                "Back-off restarting failed container postgres in pod "
                f"{_ORDERS_DB_POD}"
            ),
            "object": f"pod/{_ORDERS_DB_POD}",
            "count": 23,
            "last_seen": "2026-11-14T09:45:02Z",
        },
        {
            "type": "Warning",
            "reason": "Failed",
            "message": (
                'Error: secret "orders-db-credentials" not found: key "DB_PASSWORD" '
                "is missing from the secret data"
            ),
            "object": f"pod/{_ORDERS_DB_POD}",
            "count": 11,
            "last_seen": "2026-11-14T09:44:55Z",
        },
    ],
    logs="\n".join(
        [
            "2026-11-14T09:43:41Z INFO  entrypoint: validating environment",
            "2026-11-14T09:43:41Z FATAL environment variable DB_PASSWORD is not set",
            "2026-11-14T09:43:41Z FATAL could not read secret orders-db-credentials (key DB_PASSWORD missing)",
            "2026-11-14T09:43:41Z ERROR initdb: could not read password, refusing to start",
            "2026-11-14T09:43:41Z FATAL exiting with code 1",
            "2026-11-14T09:43:42Z INFO  restarting in 10s (attempt 11)",
        ]
    ),
)


# ---------------------------------------------------------------------------
# Scenario (d): CPU saturation / throttling
# ---------------------------------------------------------------------------
_CPU_SPIKE_TEMPLATE = IncidentTemplate(
    incident_type=MockIncidentType.CPU_SPIKE_WORKER_SATURATION,
    title="video-transcoder saturates worker-2 CPU",
    description=(
        "The transcoder deployment runs at its CPU limit, worker-2 is pinned at "
        "98% utilisation and liveness probes time out. Request latency is burning "
        "the availability SLO."
    ),
    severity="high",
    namespace="default",
    workload="video-transcoder",
    pod_name=_CPU_SPIKE_POD,
    node="worker-2",
    signals=[
        "pod Running but not Ready",
        "liveness probe timeouts",
        "CPU throttling 63.4%",
        "node CPU utilisation 98.7%",
        "load average 42.1",
    ],
    host_signals={
        "cpu_percent": 98.7,
        "load_1m": 42.1,
        "cpu_throttled_percent": 63.4,
        "node_memory_percent": 71.2,
    },
    pods=[
        _pod(
            _CPU_SPIKE_POD,
            node="worker-2",
            ready=False,
            restarts=0,
            reason=None,
            image="ghcr.io/acme/video-transcoder:1.8.0",
            message="Liveness probe failed: context deadline exceeded (CPU throttled)",
            containers=[
                _container(
                    "video-transcoder",
                    ready=False,
                    restarts=0,
                    reason=None,
                    message="container is CPU throttled at 63.4% of its 500m limit",
                )
            ],
            age_seconds=3600,
        )
    ],
    events=[
        {
            "type": "Warning",
            "reason": "Unhealthy",
            "message": (
                "Liveness probe failed: Get http://10.244.2.18:9000/healthz: "
                "context deadline exceeded (client.timeout exceeded while awaiting headers)"
            ),
            "object": f"pod/{_CPU_SPIKE_POD}",
            "count": 21,
            "last_seen": "2026-11-14T09:46:10Z",
        },
        {
            "type": "Warning",
            "reason": "CPUThrottlingHigh",
            "message": (
                "63.4% throttling of CPU in container video-transcoder for pod "
                f"{_CPU_SPIKE_POD}"
            ),
            "object": f"pod/{_CPU_SPIKE_POD}",
            "count": 47,
            "last_seen": "2026-11-14T09:46:22Z",
        },
    ],
    logs="\n".join(
        [
            "2026-11-14T09:44:01Z INFO  transcoder worker starting (threads=8)",
            "2026-11-14T09:44:30Z INFO  job=8f21ae segment 118/240 encoded in 612ms",
            "2026-11-14T09:45:02Z WARN  encode latency 4.8s for 2s segment (SLO 1.5s)",
            "2026-11-14T09:45:31Z WARN  cpu throttled: 63.4% of quota, 4120 periods throttled",
            "2026-11-14T09:45:58Z ERROR liveness endpoint timed out after 5000ms",
            "2026-11-14T09:46:05Z WARN  queue depth 1840 and growing",
            "2026-11-14T09:46:10Z ERROR kubelet marked container unready after 3 probe failures",
        ]
    ),
)


_TEMPLATES: dict[str, IncidentTemplate] = {
    template.incident_type.value: template
    for template in (
        _OOM_TEMPLATE,
        _ZOMBIE_TEMPLATE,
        _DB_CONFIG_TEMPLATE,
        _CPU_SPIKE_TEMPLATE,
    )
}

#: Case-insensitive aliases so demos can trigger with friendly names.
_ALIASES: dict[str, str] = {
    "oom": MockIncidentType.OOM_KILLED_PAYMENT_SERVICE.value,
    "oomkill": MockIncidentType.OOM_KILLED_PAYMENT_SERVICE.value,
    "oomkilled": MockIncidentType.OOM_KILLED_PAYMENT_SERVICE.value,
    "payment": MockIncidentType.OOM_KILLED_PAYMENT_SERVICE.value,
    "zombie": MockIncidentType.ZOMBIE_PROCESS_LEAK.value,
    "zombie_process": MockIncidentType.ZOMBIE_PROCESS_LEAK.value,
    "pid_exhaustion": MockIncidentType.ZOMBIE_PROCESS_LEAK.value,
    "database": MockIncidentType.CRASHLOOP_DATABASE_CONFIG.value,
    "db_config": MockIncidentType.CRASHLOOP_DATABASE_CONFIG.value,
    "crashloop": MockIncidentType.CRASHLOOP_DATABASE_CONFIG.value,
    "config_error": MockIncidentType.CRASHLOOP_DATABASE_CONFIG.value,
    "config": MockIncidentType.CRASHLOOP_DATABASE_CONFIG.value,
    "cpu_spike": MockIncidentType.CPU_SPIKE_WORKER_SATURATION.value,
    "cpu-spike": MockIncidentType.CPU_SPIKE_WORKER_SATURATION.value,
    "cpuspike": MockIncidentType.CPU_SPIKE_WORKER_SATURATION.value,
    "cpu": MockIncidentType.CPU_SPIKE_WORKER_SATURATION.value,
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class SimulatedCluster:
    """Mock Kubernetes + Linux telemetry for a chosen chaos scenario."""

    def __init__(
        self, seed: int = 1337, active_incident: str | MockIncidentType = DEFAULT_INCIDENT
    ) -> None:
        self.seed = seed
        self._active: IncidentTemplate = self._resolve(active_incident)

    # -- scenario management -------------------------------------------
    @staticmethod
    def _resolve(incident_type: str | MockIncidentType) -> IncidentTemplate:
        key = (
            incident_type.value
            if isinstance(incident_type, MockIncidentType)
            else str(incident_type).strip()
        )
        # Accept "OOMKilled", "OOMKilled_Payment_Service", "CPU Spike", "cpu-spike", ...
        normalized = key.lower().replace(" ", "_").replace("-", "_")
        by_name = {name.lower(): template for name, template in _TEMPLATES.items()}
        template = (
            _TEMPLATES.get(key)
            or by_name.get(normalized)
            or _TEMPLATES.get(_ALIASES.get(normalized, ""))
        )
        if template is None:
            valid = ", ".join(sorted(_TEMPLATES))
            raise ValueError(
                f"unknown incident_type {incident_type!r}; expected one of: {valid}"
            )
        return template

    @property
    def active_incident(self) -> IncidentTemplate:
        return self._active

    @property
    def active_incident_type(self) -> str:
        return self._active.incident_type.value

    def reset(self) -> IncidentTemplate:
        """Return the cluster to the default demo scenario."""
        self._active = _TEMPLATES[DEFAULT_INCIDENT]
        return self._active

    @staticmethod
    def available_incidents() -> list[dict[str, str]]:
        return [
            {
                "incident_type": template.incident_type.value,
                "title": template.title,
                "severity": template.severity,
                "namespace": template.namespace,
            }
            for template in _TEMPLATES.values()
        ]

    def trigger_mock_incident(self, incident_type: str) -> dict[str, Any]:
        """Activate a scenario and return the structured incident alert payload.

        Raises:
            ValueError: if ``incident_type`` is not one of the known templates.
        """
        template = self._resolve(incident_type)
        self._active = template
        return {
            "alert_id": f"chaos-{uuid.uuid4().hex[:10]}",
            "incident_type": template.incident_type.value,
            "title": template.title,
            "description": template.description,
            "severity": template.severity,
            "namespace": template.namespace,
            "workload": template.workload,
            "pod": template.pod_name,
            "node": template.node,
            "signals": list(template.signals),
            "host_signals": dict(template.host_signals),
            "logs_preview": template.logs.splitlines()[-3:],
            "source": "chaos-simulator",
            "simulated": True,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
        }

    # -- collector-facing reads ----------------------------------------
    def pods(self, namespace: str = "default") -> list[dict[str, Any]]:
        """All pods the fake cluster knows about, including scenario pods."""
        pods = [dict(pod) for pod in _BASELINE_PODS]
        pods.extend(dict(pod) for pod in self._active.pods)
        return [pod for pod in pods if pod["namespace"] == namespace]

    def events(self, namespace: str = "default") -> list[dict[str, Any]]:
        if namespace != self._active.namespace:
            return []
        return [dict(event) for event in self._active.events]

    def nodes(self) -> list[dict[str, Any]]:
        """Baseline nodes with the active scenario's pressure conditions applied."""
        pressured = self._active.node_pressures
        result: list[dict[str, Any]] = []
        for base in _BASELINE_NODES:
            node = {**base, "conditions": [dict(condition) for condition in base["conditions"]]}
            for condition in node["conditions"]:
                if condition["type"] in pressured.get(node["name"], []):
                    condition["status"] = "True"
                    condition["reason"] = "KubeletHasInsufficient" + condition["type"].replace(
                        "Pressure", ""
                    )
                    condition["message"] = (
                        f"kubelet has {'memory' if condition['type'] == 'MemoryPressure' else 'pid' if condition['type'] == 'PIDPressure' else 'disk'} "
                        "pressure and may evict pods"
                    )
            result.append(node)
        return result

    def logs(self, pod_name: str, tail_lines: int = 100) -> str:
        """Logs for a pod: scenario logs for the incident pod, baseline otherwise."""
        if pod_name == self._active.pod_name:
            body = self._active.logs
        elif any(pod["name"] == pod_name for pod in _BASELINE_PODS):
            body = _BASELINE_LOGS
        else:
            body = _DEFAULT_LOGS
        return "\n".join(body.splitlines()[-max(tail_lines, 1) :])

    def host_signals(self) -> dict[str, Any]:
        """Simulated host-level symptoms (used by the agent in simulation mode)."""
        return {
            "incident_type": self._active.incident_type.value,
            "host_signals": dict(self._active.host_signals),
        }
