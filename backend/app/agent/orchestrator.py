"""Agent orchestration: evidence collection, reasoning and remediation planning.

The orchestrator is deliberately transport-agnostic.  It receives a plain
``dict`` describing the incident, gathers evidence through the tool registry
and returns a plain ``dict`` that the API layer validates into a typed model.
That keeps the reasoning layer free of FastAPI/HTTP concerns and easy to test.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.agent.prompts import SYSTEM_PROMPT, build_diagnosis_prompt
from app.agent.tools import ToolRegistry, build_registry
from app.collectors.k8s_collector import KubernetesCollector
from app.collectors.linux_collector import LinuxCollector
from app.collectors.simulator import SimulatedCluster
from app.config import Settings

logger = logging.getLogger(__name__)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(low, min(high, number))


def _extract_json(raw: str) -> dict[str, Any]:
    """Parse a JSON object out of an LLM reply, tolerating code fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_OBJECT.search(text)
        if match is None:
            raise ValueError("LLM response did not contain a JSON object") from None
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not a JSON object")
    return parsed


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


class AgentOrchestrator:
    """Runs the collect → reason → recommend loop for a single incident."""

    def __init__(
        self,
        settings: Settings,
        *,
        k8s: KubernetesCollector | None = None,
        linux: LinuxCollector | None = None,
    ) -> None:
        self.settings = settings
        self.simulator = SimulatedCluster()
        self.k8s = k8s or KubernetesCollector(settings, self.simulator)
        self.linux = linux or LinuxCollector()
        self.tools: ToolRegistry = build_registry(self.k8s, self.linux)

    # ------------------------------------------------------------------
    # Evidence collection
    # ------------------------------------------------------------------
    async def collect_evidence(self, incident: dict[str, Any]) -> dict[str, Any]:
        """Run every read-only tool that is relevant to the incident."""
        namespace = str(incident.get("namespace") or "default")
        workload = incident.get("workload")

        calls: list[tuple[str, dict[str, Any]]] = [
            ("k8s.list_pods", {"namespace": namespace}),
            ("k8s.list_events", {"namespace": namespace}),
            ("k8s.node_status", {}),
            ("linux.host_metrics", {}),
        ]
        if workload:
            calls.append(
                (
                    "k8s.pod_logs",
                    {"namespace": namespace, "pod_name": str(workload), "tail_lines": 120},
                )
            )

        evidence: dict[str, Any] = {}
        for name, arguments in calls[: self.settings.agent_max_steps + 8]:
            try:
                evidence[name] = await self.tools.execute(name, arguments)
            except Exception as exc:  # a single failing collector must not abort the run
                logger.warning("Tool %s failed: %s", name, exc)
                evidence[name] = {"error": str(exc)}
        return evidence

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------
    async def diagnose(self, incident: dict[str, Any]) -> dict[str, Any]:
        """Return a diagnosis dict ready for ``Diagnosis.model_validate``."""
        evidence = await self.collect_evidence(incident)

        if self.settings.llm_enabled:
            try:
                return await self._llm_diagnosis(incident, evidence)
            except Exception as exc:
                logger.warning(
                    "LLM diagnosis failed (%s); falling back to the rule engine", exc
                )

        return self._rule_based_diagnosis(incident, evidence)

    async def _llm_diagnosis(
        self, incident: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any]:
        settings = self.settings
        url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_diagnosis_prompt(incident, evidence)},
            ],
        }
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key_value}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("unexpected LLM response shape") from exc

        parsed = _extract_json(content)
        severity = str(parsed.get("severity", "medium")).strip().lower()
        if severity not in _VALID_SEVERITIES:
            severity = "medium"

        return {
            "summary": str(parsed.get("summary", "")).strip()
            or "The model returned no summary.",
            "root_cause": str(parsed.get("root_cause", "")).strip() or "Undetermined.",
            "severity": severity,
            "confidence": _clamp(parsed.get("confidence", 0.6)),
            "evidence": _string_list(parsed.get("evidence")),
            "recommended_actions": _string_list(parsed.get("recommended_actions")),
            "commands": _string_list(parsed.get("commands")),
            "generated_by": f"llm:{settings.llm_model}",
            "created_at": _utcnow_iso(),
        }

    # ------------------------------------------------------------------
    # Deterministic fallback reasoning
    # ------------------------------------------------------------------
    def _rule_based_diagnosis(
        self, incident: dict[str, Any], evidence: dict[str, Any]
    ) -> dict[str, Any]:
        namespace = str(incident.get("namespace") or "default")
        pods = evidence.get("k8s.list_pods")
        events = evidence.get("k8s.list_events")
        nodes = evidence.get("k8s.node_status")
        host = evidence.get("linux.host_metrics")

        pods = pods if isinstance(pods, list) else []
        events = events if isinstance(events, list) else []
        nodes = nodes if isinstance(nodes, list) else []
        host = host if isinstance(host, dict) and "error" not in host else None

        oom_pods = [
            pod
            for pod in pods
            if "oom" in str(pod.get("last_state") or "").lower()
            or "oom" in str(pod.get("reason") or "").lower()
        ]
        crashloop_pods = [
            pod for pod in pods if "crashloop" in str(pod.get("reason") or "").lower()
        ]
        image_pods = [
            pod
            for pod in pods
            if any(
                token in str(pod.get("reason") or "").lower()
                for token in ("imagepull", "errimagepull", "invalidimagename")
            )
        ]
        not_ready_pods = [
            pod
            for pod in pods
            if not pod.get("ready") and str(pod.get("phase") or "").lower() == "running"
        ]
        pending_pods = [
            pod for pod in pods if str(pod.get("phase") or "").lower() == "pending"
        ]

        node_pressure: list[str] = []
        for node in nodes:
            for condition in node.get("conditions") or []:
                if (
                    str(condition.get("type")) in {"MemoryPressure", "DiskPressure", "PIDPressure"}
                    and str(condition.get("status")) == "True"
                ):
                    node_pressure.append(
                        f"node {node.get('name')} reports {condition.get('type')}"
                    )

        host_memory_pct = 0.0
        host_disk_pct = 0.0
        if host:
            host_memory_pct = float((host.get("memory") or {}).get("percent") or 0.0)
            host_disk_pct = float((host.get("disk_root") or {}).get("percent") or 0.0)

        warning_events = [
            event
            for event in events
            if str(event.get("type") or "").lower() == "warning"
        ][:5]

        evidence_lines: list[str] = []
        for pod in oom_pods[:3]:
            evidence_lines.append(
                f"pod {pod.get('name')} last terminated with "
                f"{pod.get('last_state')} after {pod.get('restarts')} restarts"
            )
        for pod in crashloop_pods[:3]:
            evidence_lines.append(
                f"pod {pod.get('name')} is in CrashLoopBackOff on node {pod.get('node')}"
            )
        for pod in image_pods[:3]:
            evidence_lines.append(
                f"pod {pod.get('name')} cannot pull image {pod.get('image')}"
            )
        for pod in not_ready_pods[:3]:
            evidence_lines.append(f"pod {pod.get('name')} is Running but not Ready")
        for pod in pending_pods[:3]:
            evidence_lines.append(f"pod {pod.get('name')} is Pending (unscheduled)")
        evidence_lines.extend(node_pressure[:3])
        if host:
            evidence_lines.append(
                f"host memory at {host_memory_pct:.1f}% and root disk at {host_disk_pct:.1f}%"
            )
        for event in warning_events:
            evidence_lines.append(
                f"event {event.get('reason')}: {str(event.get('message') or '')[:160]}"
            )

        # --- Root cause selection -------------------------------------
        actions: list[str] = []
        commands: list[str] = []
        confidence = 0.45
        healthy = len(pods) - len(
            {str(pod.get("name")) for pod in oom_pods + crashloop_pods + image_pods + not_ready_pods + pending_pods}
        )
        healthy = max(healthy, 0)

        if oom_pods:
            pod = oom_pods[0]
            root_cause = (
                f"Container memory exhaustion: {pod.get('name')} was OOMKilled "
                f"by the kubelet on node {pod.get('node')}."
            )
            severity = "critical"
            confidence = 0.78
            actions = [
                f"Inspect the memory limit and recent usage of {pod.get('name')}.",
                f"Raise the memory limit/request for the {namespace} workload or fix the leak.",
                "Roll the deployment once the limit is corrected and confirm restarts stop.",
            ]
            commands = [
                f"kubectl -n {namespace} describe pod {pod.get('name')}",
                f"kubectl -n {namespace} set resources deployment/{str(pod.get('name')).rsplit('-', 2)[0]} "
                "--limits=memory=1Gi --requests=memory=512Mi",
                f"kubectl -n {namespace} rollout status deployment/{str(pod.get('name')).rsplit('-', 2)[0]}",
            ]
        elif node_pressure:
            root_cause = (
                "Node-level resource pressure: " + "; ".join(node_pressure) + "."
            )
            severity = "critical"
            confidence = 0.7
            actions = [
                "Cordon the pressured node so new pods are not scheduled onto it.",
                "Evict or move non-critical workloads off the node.",
                "Verify disk/inode usage and reclaim space before uncordoning.",
            ]
            commands = [
                "kubectl get nodes -o wide",
                "kubectl describe node <pressured-node> | grep -A5 Conditions",
                "kubectl cordon <pressured-node>",
            ]
        elif crashloop_pods:
            pod = crashloop_pods[0]
            root_cause = (
                f"Crash loop in {pod.get('name')}: the container exits repeatedly "
                f"(reason {pod.get('reason')}) without a recorded OOM kill."
            )
            severity = "high"
            confidence = 0.62
            actions = [
                f"Read the previous container logs for {pod.get('name')}.",
                "Check configuration and mounted secrets referenced by the pod.",
                "Roll the deployment after correcting the configuration.",
            ]
            commands = [
                f"kubectl -n {namespace} logs {pod.get('name')} --previous --tail=200",
                f"kubectl -n {namespace} describe pod {pod.get('name')}",
                f"kubectl -n {namespace} rollout restart deployment/{str(pod.get('name')).rsplit('-', 2)[0]}",
            ]
        elif image_pods:
            pod = image_pods[0]
            root_cause = (
                f"Image pull failure: {pod.get('name')} cannot pull {pod.get('image')}."
            )
            severity = "high"
            confidence = 0.8
            actions = [
                "Verify the image reference and tag exist in the registry.",
                "Check whether the imagePullSecret is present and still valid.",
            ]
            commands = [
                f"kubectl -n {namespace} describe pod {pod.get('name')}",
                f"kubectl -n {namespace} get secret",
            ]
        elif pending_pods:
            pod = pending_pods[0]
            root_cause = (
                f"Unschedulable workload: {pod.get('name')} is Pending, usually a "
                "resource request or taint/toleration mismatch."
            )
            severity = "medium"
            confidence = 0.55
            actions = [
                "Inspect the scheduler events for the pending pod.",
                "Compare requests/limits against free capacity on the nodes.",
            ]
            commands = [
                f"kubectl -n {namespace} describe pod {pod.get('name')}",
                "kubectl describe nodes | grep -A5 'Allocated resources'",
            ]
        elif host and host_memory_pct >= 85.0:
            root_cause = (
                f"Host memory pressure on the agent node itself ({host_memory_pct:.1f}% used)."
            )
            severity = "high"
            confidence = 0.5
            actions = [
                "Identify the top memory consumers on the host.",
                "Free memory or add capacity before the kernel starts OOM killing pods.",
            ]
            commands = [
                "ps aux --sort=-%mem | head -n 10",
                "free -h",
            ]
        elif host and host_disk_pct >= 85.0:
            root_cause = f"Host disk pressure ({host_disk_pct:.1f}% used on /)."
            severity = "medium"
            confidence = 0.5
            actions = ["Locate large files and prune old container images/logs."]
            commands = ["df -h", "du -xh / --max-depth=1 | sort -h | tail -n 10"]
        else:
            root_cause = (
                "No active failure signal found in the collected evidence; the "
                "reported incident may already be resolved."
            )
            severity = "low"
            confidence = 0.35
            actions = [
                "Confirm the described user impact is still occurring.",
                "Re-run the analysis with a narrower namespace or workload filter.",
            ]
            commands = [f"kubectl -n {namespace} get pods -o wide"]

        summary = (
            f"{len(pods) - healthy} of {len(pods)} pods in namespace '{namespace}' "
            f"show unhealthy signals; {len(warning_events)} warning events in the "
            f"recent event stream."
        )
        if not evidence_lines:
            evidence_lines = [
                "No OOM, crash loop, image pull or node pressure signal was observed."
            ]

        return {
            "summary": summary,
            "root_cause": root_cause,
            "severity": severity,
            "confidence": confidence,
            "evidence": evidence_lines[:12],
            "recommended_actions": actions,
            "commands": commands,
            "generated_by": "rules:builtin",
            "created_at": _utcnow_iso(),
        }
