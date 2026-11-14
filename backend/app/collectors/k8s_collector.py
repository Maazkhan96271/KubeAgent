"""Kubernetes evidence collection.

In ``SIMULATION_MODE`` the collector never talks to a cluster: it serves the
deterministic scenario from :mod:`app.collectors.simulator`.  Otherwise it uses
the official Kubernetes Python client, lazily loading credentials on first use.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from app.collectors.simulator import SimulatedCluster
from app.config import Settings

logger = logging.getLogger(__name__)


class KubernetesUnavailable(RuntimeError):
    """Raised when the Kubernetes API cannot be configured or reached."""


def _age_seconds(created: datetime | None) -> int | None:
    if created is None:
        return None
    reference = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return max(int((datetime.now(timezone.utc) - reference).total_seconds()), 0)


class KubernetesCollector:
    """Read-only Kubernetes queries used as agent tools."""

    def __init__(
        self, settings: Settings, simulator: SimulatedCluster | None = None
    ) -> None:
        self._settings = settings
        self._simulator = simulator or SimulatedCluster()
        self._core: client.CoreV1Api | None = None

    # ------------------------------------------------------------------
    # Client bootstrap
    # ------------------------------------------------------------------
    @property
    def simulation_mode(self) -> bool:
        return self._settings.simulation_mode

    def _api(self) -> client.CoreV1Api:
        if self._core is not None:
            return self._core
        try:
            if self._settings.kubeconfig_path:
                config.load_kube_config(
                    config_file=str(self._settings.kubeconfig_path),
                    context=self._settings.kube_context,
                )
            else:
                try:
                    config.load_incluster_config()
                except Exception:
                    config.load_kube_config(context=self._settings.kube_context)
            self._core = client.CoreV1Api()
        except Exception as exc:
            raise KubernetesUnavailable(
                f"unable to load Kubernetes credentials: {exc}"
            ) from exc
        return self._core

    # ------------------------------------------------------------------
    # Pods
    # ------------------------------------------------------------------
    def list_pods(self, namespace: str = "default") -> list[dict[str, Any]]:
        """List pods in a namespace with the fields the agent reasons about."""
        if self.simulation_mode:
            return self._simulator.pods(namespace)
        api = self._api()
        try:
            pods = api.list_namespaced_pod(
                namespace=namespace, _request_timeout=self._settings.k8s_timeout_seconds
            ).items
        except ApiException as exc:
            raise KubernetesUnavailable(
                f"listing pods in '{namespace}' failed: {exc.reason}"
            ) from exc
        return [self._serialise_pod(pod) for pod in pods]

    @staticmethod
    def _serialise_pod(pod: Any) -> dict[str, Any]:
        status = pod.status
        container_statuses = status.container_statuses or []
        restarts = sum(int(cs.restart_count or 0) for cs in container_statuses)

        waiting: str | None = None
        terminated: str | None = None
        last_terminated: str | None = None
        for cs in container_statuses:
            if cs.state and cs.state.waiting:
                waiting = cs.state.waiting.reason
            if cs.state and cs.state.terminated:
                terminated = cs.state.terminated.reason
            if cs.last_state and cs.last_state.terminated:
                last_terminated = cs.last_state.terminated.reason

        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": status.phase,
            "reason": waiting or terminated or status.reason,
            "last_state": last_terminated or terminated,
            "ready": all(bool(cs.ready) for cs in container_statuses)
            if container_statuses
            else False,
            "restarts": restarts,
            "node": pod.spec.node_name if pod.spec else None,
            "image": pod.spec.containers[0].image
            if pod.spec and pod.spec.containers
            else None,
            "age_seconds": _age_seconds(pod.metadata.creation_timestamp),
        }

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def list_events(self, namespace: str = "default", limit: int = 50) -> list[dict[str, Any]]:
        """Return recent namespace events, warnings first."""
        limit = max(int(limit), 1)
        if self.simulation_mode:
            return self._simulator.events(namespace)[:limit]
        api = self._api()
        try:
            events = api.list_namespaced_event(
                namespace=namespace, _request_timeout=self._settings.k8s_timeout_seconds
            ).items
        except ApiException as exc:
            raise KubernetesUnavailable(
                f"listing events in '{namespace}' failed: {exc.reason}"
            ) from exc

        serialised = [
            {
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "object": f"{str(event.involved_object.kind or '').lower()}/"
                f"{event.involved_object.name}"
                if event.involved_object
                else None,
                "count": event.count,
                "last_timestamp": event.last_timestamp.isoformat()
                if event.last_timestamp
                else None,
            }
            for event in events
        ]
        serialised.sort(key=lambda item: item["type"] != "Warning")
        return serialised[:limit]

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------
    def node_status(self) -> list[dict[str, Any]]:
        """Return nodes with their pressure conditions and capacity."""
        if self.simulation_mode:
            return self._simulator.nodes()
        api = self._api()
        try:
            nodes = api.list_node(_request_timeout=self._settings.k8s_timeout_seconds).items
        except ApiException as exc:
            raise KubernetesUnavailable(f"listing nodes failed: {exc.reason}") from exc

        result: list[dict[str, Any]] = []
        for node in nodes:
            conditions = [
                {"type": condition.type, "status": condition.status}
                for condition in (node.status.conditions or [])
            ]
            capacity = node.status.capacity or {}
            result.append(
                {
                    "name": node.metadata.name,
                    "ready": any(
                        c["type"] == "Ready" and c["status"] == "True"
                        for c in conditions
                    ),
                    "roles": sorted(
                        label.split("/", 1)[1]
                        for label in (node.metadata.labels or {})
                        if label.startswith("node-role.kubernetes.io/")
                    ),
                    "conditions": conditions,
                    "capacity": {
                        "cpu": capacity.get("cpu"),
                        "memory": capacity.get("memory"),
                        "pods": capacity.get("pods"),
                    },
                }
            )
        return result

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------
    def pod_logs(
        self, pod_name: str, namespace: str = "default", tail_lines: int = 100
    ) -> str:
        """Tail one container's logs from a pod."""
        tail_lines = max(int(tail_lines), 1)
        if self.simulation_mode:
            return self._simulator.logs(pod_name, tail_lines=tail_lines)
        api = self._api()
        try:
            return api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=tail_lines,
                _request_timeout=self._settings.k8s_timeout_seconds,
            )
        except ApiException as exc:
            raise KubernetesUnavailable(
                f"reading logs for '{pod_name}' failed: {exc.reason}"
            ) from exc
