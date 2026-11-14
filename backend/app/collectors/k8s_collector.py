"""Kubernetes evidence collection.

Credential loading is defensive: an explicit kubeconfig, then the in-cluster
service account, then the local ``~/.kube/config``.  Every failure is collected
into one actionable :class:`KubernetesUnavailable` error, so running outside a
cluster produces a clear message instead of a stack trace.  With
``SIMULATION_MODE`` enabled the collector never touches the API and serves the
chaos simulator instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

from app.collectors.simulator import SimulatedCluster
from app.config import Settings

logger = logging.getLogger(__name__)

#: Container waiting reasons that always mean the pod needs attention.
BAD_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "crashloopbackoff",
        "imagepullbackoff",
        "errimagepull",
        "createcontainerconfigerror",
        "invalidimagename",
        "runcontainererror",
        "containercreating",
    }
)

#: Terminated reasons that mean the container died abnormally.
BAD_TERMINATED_REASONS: frozenset[str] = frozenset({"oomkilled", "error", "evicted"})

#: Pod phases that are never healthy.
BAD_PHASES: frozenset[str] = frozenset({"pending", "failed", "unknown"})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class NodeCondition(BaseModel):
    type: str
    status: str
    reason: str | None = None
    message: str | None = None


class NodeInfo(BaseModel):
    """Health of one cluster node."""

    name: str
    ready: bool = False
    roles: list[str] = Field(default_factory=list)
    conditions: list[NodeCondition] = Field(default_factory=list)
    capacity: dict[str, str] = Field(default_factory=dict)
    allocatable: dict[str, str] = Field(default_factory=dict)
    memory_pressure: bool = False
    disk_pressure: bool = False
    pid_pressure: bool = False
    kubernetes_version: str | None = None
    age_seconds: int | None = None


class ContainerStatus(BaseModel):
    name: str
    ready: bool = False
    restarts: int = 0
    reason: str | None = None
    exit_code: int | None = None
    message: str | None = None


class PodIssue(BaseModel):
    """An unhealthy pod with the container states that explain why."""

    name: str
    namespace: str
    node: str | None = None
    phase: str = "Unknown"
    ready: bool = False
    restarts: int = 0
    reason: str | None = None
    last_state: str | None = None
    exit_code: int | None = None
    image: str | None = None
    age_seconds: int | None = None
    containers: list[ContainerStatus] = Field(default_factory=list)
    message: str | None = None


class UnhealthyPodsReport(BaseModel):
    """Result of :meth:`KubernetesCollector.get_unhealthy_pods`."""

    namespace: str
    total_pods: int = 0
    unhealthy_count: int = 0
    issues: list[PodIssue] = Field(default_factory=list)
    simulation: bool = False
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PodLogsResult(BaseModel):
    """Result of :meth:`KubernetesCollector.get_pod_logs`."""

    pod_name: str
    namespace: str
    container: str | None = None
    tail_lines: int = 100
    line_count: int = 0
    logs: str = ""
    truncated: bool = False
    simulation: bool = False
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PodEvent(BaseModel):
    """One Kubernetes event attached to a pod."""

    type: str
    reason: str | None = None
    message: str | None = None
    object: str | None = None
    count: int | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class PodEventsReport(BaseModel):
    """Result of :meth:`KubernetesCollector.describe_pod_events`."""

    pod_name: str
    namespace: str
    event_count: int = 0
    events: list[PodEvent] = Field(default_factory=list)
    simulation: bool = False
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KubernetesUnavailable(RuntimeError):
    """Raised when the Kubernetes API cannot be configured, reached or queried."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _age_seconds(created: datetime | None) -> int | None:
    if created is None:
        return None
    reference = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return max(int((datetime.now(timezone.utc) - reference).total_seconds()), 0)


def _is_unhealthy(issue: PodIssue) -> bool:
    """Shared health rule for live and simulated pods."""
    if str(issue.phase).lower() in BAD_PHASES:
        return True
    reasons = {str(issue.reason or "").lower()}
    reasons.update(str(container.reason or "").lower() for container in issue.containers)
    if reasons & BAD_WAITING_REASONS:
        return True
    if str(issue.last_state or "").lower() in BAD_TERMINATED_REASONS:
        return True
    if issue.phase == "Running" and not issue.ready:
        return True
    return False


def _string_dict(values: Any) -> dict[str, str]:
    """Coerce a Kubernetes resource quantity mapping into ``{str: str}``."""
    if not isinstance(values, dict):
        return {}
    return {str(key): str(value) for key, value in values.items()}


def _pod_issue(pod: Any) -> PodIssue:
    """Convert a live ``V1Pod`` into a :class:`PodIssue`."""
    status = pod.status
    phase = status.phase or "Unknown"
    containers: list[ContainerStatus] = []

    for container in status.container_statuses or []:
        waiting = container.state.waiting if container.state and container.state.waiting else None
        terminated = (
            container.state.terminated if container.state and container.state.terminated else None
        )
        last_terminated = (
            container.last_state.terminated
            if container.last_state and container.last_state.terminated
            else None
        )
        reason = waiting.reason if waiting else (terminated.reason if terminated else None)
        message = waiting.message if waiting else (terminated.message if terminated else None)
        exit_code = terminated.exit_code if terminated else None

        containers.append(
            ContainerStatus(
                name=container.name,
                ready=bool(container.ready),
                restarts=int(container.restart_count or 0),
                reason=reason,
                exit_code=int(exit_code) if exit_code is not None else None,
                message=(message or "")[:500] or None,
            )
        )

    last_state: str | None = None
    for container in status.container_statuses or []:
        if container.last_state and container.last_state.terminated:
            last_state = container.last_state.terminated.reason
            break
        if container.state and container.state.terminated:
            last_state = container.state.terminated.reason

    exit_code = next(
        (container.exit_code for container in containers if container.exit_code is not None),
        None,
    )
    reason = next((container.reason for container in containers if container.reason), None)
    if reason is None and status.reason:
        reason = status.reason
    message = next((container.message for container in containers if container.message), None)

    return PodIssue(
        name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        node=pod.spec.node_name if pod.spec else None,
        phase=phase,
        ready=bool(containers) and all(container.ready for container in containers),
        restarts=sum(container.restarts for container in containers),
        reason=reason,
        last_state=last_state or status.reason,
        exit_code=exit_code,
        image=(pod.spec.containers[0].image if pod.spec and pod.spec.containers else None),
        age_seconds=_age_seconds(pod.metadata.creation_timestamp),
        containers=containers,
        message=message,
    )


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
class KubernetesCollector:
    """Read-only Kubernetes queries used as agent tools."""

    def __init__(
        self, settings: Settings, simulator: SimulatedCluster | None = None
    ) -> None:
        self._settings = settings
        self._simulator = simulator or SimulatedCluster()
        self._core: client.CoreV1Api | None = None

    @property
    def simulation_mode(self) -> bool:
        return self._settings.simulation_mode

    @property
    def simulator(self) -> SimulatedCluster:
        """The chaos engine backing this collector (shared with the agent)."""
        return self._simulator

    # ------------------------------------------------------------------
    # Client bootstrap
    # ------------------------------------------------------------------
    def _client(self) -> client.CoreV1Api:
        """Load credentials in order of preference and cache the API client."""
        if self._core is not None:
            return self._core

        attempts: list[tuple[str, Callable[[], None]]] = []
        if self._settings.kubeconfig_path:
            explicit = self._settings.kubeconfig_path
            attempts.append(
                (
                    f"kubeconfig {explicit}",
                    lambda: config.load_kube_config(
                        config_file=str(explicit), context=self._settings.kube_context
                    ),
                )
            )
        attempts.append(("in-cluster service account", config.load_incluster_config))
        attempts.append(
            (
                "local kubeconfig (~/.kube/config)",
                lambda: config.load_kube_config(context=self._settings.kube_context),
            )
        )

        errors: list[str] = []
        for label, loader in attempts:
            try:
                loader()
                self._core = client.CoreV1Api()
                logger.info("Kubernetes client initialised via %s", label)
                return self._core
            except Exception as exc:  # noqa: BLE001 - every loader fails differently
                errors.append(f"{label}: {exc}")
                logger.debug("Kubernetes credential source failed (%s): %s", label, exc)

        raise KubernetesUnavailable(
            "no usable Kubernetes credentials found. Set SIMULATION_MODE=true to use "
            "the chaos simulator, or provide KUBECONFIG_PATH / an in-cluster service "
            "account. Attempts: " + " | ".join(errors)
        )

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------
    def get_cluster_nodes(self) -> list[NodeInfo]:
        """Node health with Ready/MemoryPressure/DiskPressure/PIDPressure."""
        if self.simulation_mode:
            return [NodeInfo.model_validate(node) for node in self._simulator.nodes()]

        api = self._client()
        try:
            nodes = api.list_node(
                _request_timeout=self._settings.k8s_timeout_seconds
            ).items
        except ApiException as exc:
            raise KubernetesUnavailable(f"listing nodes failed: {exc.reason}") from exc

        result: list[NodeInfo] = []
        for node in nodes:
            conditions = [
                NodeCondition(
                    type=str(condition.type),
                    status=str(condition.status),
                    reason=condition.reason,
                    message=(condition.message or "")[:500] or None,
                )
                for condition in (node.status.conditions or [])
            ]
            status_map = {condition.type: condition.status for condition in conditions}
            labels = node.metadata.labels or {}
            result.append(
                NodeInfo(
                    name=node.metadata.name,
                    ready=status_map.get("Ready") == "True",
                    roles=sorted(
                        label.split("/", 1)[1]
                        for label in labels
                        if label.startswith("node-role.kubernetes.io/")
                    ),
                    conditions=conditions,
                    capacity=_string_dict(node.status.capacity),
                    allocatable=_string_dict(node.status.allocatable),
                    memory_pressure=status_map.get("MemoryPressure") == "True",
                    disk_pressure=status_map.get("DiskPressure") == "True",
                    pid_pressure=status_map.get("PIDPressure") == "True",
                    kubernetes_version=(node.status.node_info.kubelet_version if node.status.node_info else None),
                    age_seconds=_age_seconds(node.metadata.creation_timestamp),
                )
            )
        return result

    # ------------------------------------------------------------------
    # Pods
    # ------------------------------------------------------------------
    def get_unhealthy_pods(self, namespace: str = "default") -> UnhealthyPodsReport:
        """Pods that are CrashLoopBackOff, OOMKilled, Pending, Error or not Ready."""
        if self.simulation_mode:
            pods = self._simulator.pods(namespace)
            issues = [
                issue
                for pod in pods
                if _is_unhealthy(issue := PodIssue.model_validate(pod))
            ]
            return UnhealthyPodsReport(
                namespace=namespace,
                total_pods=len(pods),
                unhealthy_count=len(issues),
                issues=issues,
                simulation=True,
            )

        api = self._client()
        try:
            pods = api.list_namespaced_pod(
                namespace=namespace, _request_timeout=self._settings.k8s_timeout_seconds
            ).items
        except ApiException as exc:
            raise KubernetesUnavailable(
                f"listing pods in '{namespace}' failed: {exc.reason}"
            ) from exc

        issues = [
            issue for pod in pods if _is_unhealthy(issue := _pod_issue(pod))
        ]
        return UnhealthyPodsReport(
            namespace=namespace,
            total_pods=len(pods),
            unhealthy_count=len(issues),
            issues=issues,
            simulation=False,
        )

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------
    def get_pod_logs(
        self, pod_name: str, namespace: str = "default", tail_lines: int = 100
    ) -> PodLogsResult:
        """Fetch stdout/stderr (kubelet merges both) for a pod."""
        tail_lines = max(int(tail_lines), 1)

        if self.simulation_mode:
            logs = self._simulator.logs(pod_name, tail_lines=tail_lines)
            line_count = len(logs.splitlines())
            return PodLogsResult(
                pod_name=pod_name,
                namespace=namespace,
                tail_lines=tail_lines,
                line_count=line_count,
                logs=logs,
                truncated=line_count >= tail_lines,
                simulation=True,
            )

        api = self._client()
        try:
            logs = api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=tail_lines,
                _request_timeout=self._settings.k8s_timeout_seconds,
            )
        except ApiException as exc:
            # 400 usually means the container has not started yet (Pending).
            raise KubernetesUnavailable(
                f"reading logs for '{pod_name}' in '{namespace}' failed: "
                f"{exc.reason} ({exc.status})"
            ) from exc

        line_count = len(logs.splitlines()) if logs else 0
        return PodLogsResult(
            pod_name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            line_count=line_count,
            logs=logs or "",
            truncated=line_count >= tail_lines,
            simulation=False,
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def describe_pod_events(
        self, pod_name: str, namespace: str = "default"
    ) -> PodEventsReport:
        """Warning events referencing a specific pod."""
        if self.simulation_mode:
            events = [
                PodEvent.model_validate(event)
                for event in self._simulator.events(namespace)
                if str(event.get("object") or "").endswith(pod_name)
            ]
            return PodEventsReport(
                pod_name=pod_name,
                namespace=namespace,
                event_count=len(events),
                events=events,
                simulation=True,
            )

        api = self._client()
        try:
            raw_events = api.list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod",
                _request_timeout=self._settings.k8s_timeout_seconds,
            ).items
        except ApiException as exc:
            raise KubernetesUnavailable(
                f"listing events for '{pod_name}' in '{namespace}' failed: {exc.reason}"
            ) from exc

        events = [
            PodEvent(
                type=str(event.type or "Normal"),
                reason=event.reason,
                message=(event.message or "")[:1000] or None,
                object=f"pod/{pod_name}",
                count=event.count,
                first_seen=event.first_timestamp,
                last_seen=event.last_timestamp or event.event_time,
            )
            for event in raw_events
            if str(event.type or "").lower() == "warning"
        ]
        events.sort(key=lambda item: item.last_seen or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return PodEventsReport(
            pod_name=pod_name,
            namespace=namespace,
            event_count=len(events),
            events=events,
            simulation=False,
        )
