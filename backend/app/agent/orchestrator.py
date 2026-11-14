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
from typing import Any, Awaitable, Callable

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

#: Substrings that make a log line worth quoting as evidence.
#: Optional hook used by the API layer to stream agent progress over SSE.
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]

_LOG_HINTS = (
    "error",
    "fatal",
    "panic",
    "oom",
    "killed",
    "out of memory",
    "refused",
    "denied",
    "not set",
    "missing",
    "defunct",
    "zombie",
    "resource temporarily unavailable",
    "eagain",
    "exit code",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(low, min(high, number))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _deployment_name(pod_name: str) -> str:
    """Best-effort deployment name from a ReplicaSet-style pod name.

    StatefulSet pods (``orders-db-0``) and bare pods are returned unchanged so
    we never suggest rolling a workload that does not exist.
    """
    parts = pod_name.rsplit("-", 2)
    if len(parts) == 3 and not parts[2].isdigit():
        return parts[0]
    return pod_name


def _container_name(issue: dict[str, Any]) -> str:
    containers = _as_list(issue.get("containers"))
    return str(containers[0].get("name")) if containers else "app"


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
        self.k8s = k8s or KubernetesCollector(settings, SimulatedCluster())
        # Share the collector's chaos engine so host signals and pod data always
        # describe the same scenario.
        self.simulator = self.k8s.simulator
        self.linux = linux or LinuxCollector()
        self.tools: ToolRegistry = build_registry(self.k8s, self.linux)

    async def _notify(
        self,
        progress: ProgressCallback | None,
        phase: str,
        message: str,
        *,
        tool: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Emit a progress event; a failing listener must never break the run."""
        if progress is None:
            return
        try:
            await progress(
                {"phase": phase, "message": message, "tool": tool, "data": data or {}}
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("progress callback failed: %s", exc)

    # ------------------------------------------------------------------
    # Evidence collection
    # ------------------------------------------------------------------
    async def collect_evidence(
        self,
        incident: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Run every read-only tool that is relevant to the incident."""
        namespace = str(incident.get("namespace") or "default")
        workload = incident.get("workload")

        calls: list[tuple[str, dict[str, Any]]] = [
            ("k8s.unhealthy_pods", {"namespace": namespace}),
            ("k8s.nodes", {}),
            ("linux.system_metrics", {}),
            ("linux.zombie_processes", {}),
        ]
        if workload:
            calls += [
                (
                    "k8s.pod_logs",
                    {"namespace": namespace, "pod_name": str(workload), "tail_lines": 120},
                ),
                (
                    "k8s.pod_events",
                    {"namespace": namespace, "pod_name": str(workload)},
                ),
            ]

        evidence: dict[str, Any] = {}
        for name, arguments in calls:
            await self._notify(
                progress, "thought", f"Collecting evidence with {name}", tool=name
            )
            try:
                evidence[name] = await self.tools.execute(name, arguments)
            except Exception as exc:  # one failing collector must not abort the run
                logger.warning("Tool %s failed: %s", name, exc)
                evidence[name] = {"error": str(exc)}
                await self._notify(
                    progress,
                    "observation",
                    f"{name} failed: {exc}",
                    tool=name,
                )
                continue
            await self._notify(
                progress, "observation", f"{name} completed", tool=name
            )

        if self.settings.simulation_mode:
            evidence["chaos.simulation"] = self.simulator.host_signals()

        return evidence

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------
    async def diagnose(
        self,
        incident: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Return a diagnosis dict ready for ``Diagnosis.model_validate``."""
        await self._notify(
            progress,
            "thought",
            f"Investigating '{incident.get('title') or 'incident'}' in namespace "
            f"'{incident.get('namespace') or 'default'}'",
        )
        evidence = await self.collect_evidence(incident, progress=progress)

        if self.settings.llm_enabled:
            try:
                await self._notify(progress, "thought", "Reasoning over the evidence with the LLM")
                diagnosis = await self._llm_diagnosis(incident, evidence)
                await self._notify(
                    progress, "verification", f"Diagnosis ready: {diagnosis.get('root_cause')}"
                )
                return diagnosis
            except Exception as exc:
                logger.warning(
                    "LLM diagnosis failed (%s); falling back to the rule engine", exc
                )
                await self._notify(
                    progress, "observation", f"LLM unavailable ({exc}); using the rule engine"
                )

        diagnosis = self._rule_based_diagnosis(incident, evidence)
        await self._notify(
            progress, "verification", f"Diagnosis ready: {diagnosis.get('root_cause')}"
        )
        return diagnosis

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

        report = _as_dict(evidence.get("k8s.unhealthy_pods"))
        issues = _as_list(report.get("issues"))
        total_pods = int(_number(report.get("total_pods"), len(issues)))
        nodes = _as_list(evidence.get("k8s.nodes"))
        metrics = _as_dict(evidence.get("linux.system_metrics"))
        zombies = _as_dict(evidence.get("linux.zombie_processes"))
        logs_result = _as_dict(evidence.get("k8s.pod_logs"))
        events_report = _as_dict(evidence.get("k8s.pod_events"))
        chaos = _as_dict(evidence.get("chaos.simulation"))
        simulated_host = _as_dict(chaos.get("host_signals"))

        logs_text = str(logs_result.get("logs") or "")
        events = _as_list(events_report.get("events"))

        oom_pods = [
            issue
            for issue in issues
            if "oom" in f"{issue.get('last_state')} {issue.get('reason')}".lower()
            or issue.get("exit_code") == 137
        ]
        config_pods = [
            issue
            for issue in issues
            if any(
                token in str(issue.get("reason") or "").lower()
                for token in ("createcontainerconfigerror", "configerror", "runcontainererror")
            )
        ]
        image_pods = [
            issue
            for issue in issues
            if any(
                token in str(issue.get("reason") or "").lower()
                for token in ("imagepull", "errimagepull", "invalidimagename")
            )
        ]
        crashloop_pods = [
            issue for issue in issues if "crashloop" in str(issue.get("reason") or "").lower()
        ]
        pending_pods = [
            issue for issue in issues if str(issue.get("phase") or "").lower() == "pending"
        ]

        node_pressure = [
            f"node {node.get('name')} reports {condition.get('type')}"
            for node in nodes
            for condition in _as_list(node.get("conditions"))
            if condition.get("type")
            in {"MemoryPressure", "DiskPressure", "PIDPressure"}
            and condition.get("status") == "True"
        ]

        memory_percent = _number((_as_dict(metrics.get("memory"))).get("percent"))
        disk_percent = _number((_as_dict(metrics.get("root_disk"))).get("percent"))
        iowait = _number((_as_dict(metrics.get("cpu"))).get("iowait_percent"))
        cpu_percent = _number((_as_dict(metrics.get("cpu"))).get("percent"))

        # Simulated host symptoms stand in for a real host in demo mode.
        zombie_count = int(
            _number(
                simulated_host.get("zombie_count"),
                _number(zombies.get("zombie_count")),
            )
        )
        pid_pressure = bool(zombies.get("pid_pressure")) or zombie_count >= 20
        iowait = max(iowait, _number(simulated_host.get("cpu_iowait_percent")))
        memory_percent = max(memory_percent, _number(simulated_host.get("node_memory_percent")))
        cpu_percent = max(cpu_percent, _number(simulated_host.get("cpu_percent")))

        missing_secret_hint = any(
            token in logs_text.lower()
            for token in (
                "password is not set",
                "db_password",
                "secret",
                "missing required environment",
                "could not read secret",
            )
        )

        # --- Root cause selection -------------------------------------
        severity = "low"
        confidence = 0.4
        actions: list[str] = []
        commands: list[str] = []
        primary = issues[0] if issues else {}
        pod_name = str(primary.get("name") or "the workload")
        deployment = _deployment_name(pod_name)
        container = _container_name(primary)

        if oom_pods:
            issue = oom_pods[0]
            pod = str(issue.get("name"))
            root_cause = (
                f"Container memory exhaustion: {pod} was OOMKilled (exit code "
                f"{issue.get('exit_code') or 137}) after {issue.get('restarts')} restarts."
            )
            severity = "critical"
            confidence = 0.8
            actions = [
                f"Confirm the container memory limit and the JVM/heap settings for {pod}.",
                f"Raise the memory limit of deployment/{_deployment_name(pod)} or fix the allocation leak.",
                "Roll the deployment and verify restart counts stop increasing for 15 minutes.",
            ]
            commands = [
                f"kubectl -n {namespace} describe pod {pod}",
                f"kubectl -n {namespace} logs {pod} --previous --tail=200",
                f"kubectl -n {namespace} set resources deployment/{_deployment_name(pod)} "
                "--limits=memory=1Gi --requests=memory=512Mi",
                f"kubectl -n {namespace} rollout status deployment/{_deployment_name(pod)}",
            ]
        elif pid_pressure or zombie_count >= 20:
            root_cause = (
                f"PID exhaustion from unreaped child processes: {zombie_count} defunct "
                f"processes on the host, iowait at {iowait:.1f}%."
            )
            severity = "high"
            confidence = 0.74
            actions = [
                f"Confirm the defunct children belong to {pod_name} and identify their parent PID.",
                "Fix the workload: install a SIGCHLD handler / use subprocess with wait() so children are reaped.",
                "Restart the leaking container to clear the zombie table, then watch fork() errors disappear.",
            ]
            commands = [
                "ps -eo pid,ppid,stat,comm | awk '$3 ~ /Z/ {print}' | head -n 20",
                "ps -eo stat | grep -c '^Z'",
                "cat /proc/sys/kernel/pid_max",
                f"kubectl -n {namespace} rollout restart deployment/{deployment}",
            ]
        elif node_pressure:
            root_cause = "Node-level resource pressure: " + "; ".join(node_pressure) + "."
            severity = "critical"
            confidence = 0.7
            actions = [
                "Cordon the pressured node so new pods are not scheduled onto it.",
                "Drain or evict non-critical workloads off the node.",
                "Reclaim memory/PIDs/disk space, then uncordon and confirm conditions clear.",
            ]
            commands = [
                "kubectl get nodes -o wide",
                "kubectl describe node <pressured-node> | grep -A5 Conditions",
                "kubectl cordon <pressured-node>",
            ]
        elif config_pods or missing_secret_hint:
            root_cause = (
                f"Configuration error: {pod_name} cannot start because a required "
                "secret or environment variable (for example DB_PASSWORD) is missing."
            )
            severity = "high"
            confidence = 0.72
            actions = [
                f"Inspect the secret keys referenced by deployment/{deployment}, then confirm DB_PASSWORD exists.",
                "Create or patch the missing secret key, then delete the failing pod so it is recreated.",
                "Verify the pod reaches Running/Ready and that the database accepts connections.",
            ]
            commands = [
                f"kubectl -n {namespace} get secret -o json | jq '.items[].metadata.name'",
                f"kubectl -n {namespace} describe pod {pod_name}",
                f"kubectl -n {namespace} logs {pod_name} --previous --tail=100",
                f"kubectl -n {namespace} delete pod {pod_name}",
            ]
        elif image_pods:
            issue = image_pods[0]
            root_cause = (
                f"Image pull failure: {issue.get('name')} cannot pull {issue.get('image')}."
            )
            severity = "high"
            confidence = 0.8
            actions = [
                "Verify the image reference and tag exist in the registry.",
                "Check that the imagePullSecret is present and still valid.",
            ]
            commands = [
                f"kubectl -n {namespace} describe pod {issue.get('name')}",
                f"kubectl -n {namespace} get secret",
            ]
        elif cpu_percent >= 85.0:
            root_cause = (
                f"CPU saturation: the node is at {cpu_percent:.1f}% CPU utilisation and "
                f"{pod_name} is being throttled, which breaks its liveness probes."
            )
            severity = "high"
            confidence = 0.7
            actions = [
                f"Compare the CPU requests/limits of {pod_name} with the node capacity.",
                "Scale the workload out or raise its CPU limit so throttling stops.",
                "Move non-critical workloads off the saturated node.",
            ]
            commands = [
                f"kubectl -n {namespace} top pod {pod_name}",
                f"kubectl -n {namespace} describe pod {pod_name} | grep -A3 Limits",
                f"kubectl -n {namespace} scale deployment/{deployment} --replicas=4",
            ]
        elif crashloop_pods:
            issue = crashloop_pods[0]
            pod = str(issue.get("name"))
            root_cause = (
                f"Crash loop in {pod}: the container exits repeatedly "
                f"(exit code {issue.get('exit_code')}, reason {issue.get('reason')}) "
                "without a recorded OOM kill."
            )
            severity = "high"
            confidence = 0.6
            actions = [
                f"Read the previous container logs for {pod} to find the failing startup step.",
                "Check configuration, mounted secrets and dependency reachability.",
                "Roll the deployment once the configuration is corrected.",
            ]
            commands = [
                f"kubectl -n {namespace} logs {pod} --previous --tail=200",
                f"kubectl -n {namespace} describe pod {pod}",
                f"kubectl -n {namespace} rollout restart deployment/{_deployment_name(pod)}",
            ]
        elif pending_pods:
            issue = pending_pods[0]
            root_cause = (
                f"Unschedulable workload: {issue.get('name')} is Pending, usually a "
                "resource request or taint/toleration mismatch."
            )
            severity = "medium"
            confidence = 0.55
            actions = [
                "Inspect the scheduler events for the pending pod.",
                "Compare requests/limits against free capacity on the nodes.",
            ]
            commands = [
                f"kubectl -n {namespace} describe pod {issue.get('name')}",
                "kubectl describe nodes | grep -A5 'Allocated resources'",
            ]
        elif issues and not issues[0].get("ready", True):
            root_cause = (
                f"{pod_name} is Running but not Ready; probe or dependency failures "
                "are blocking readiness."
            )
            severity = "medium"
            confidence = 0.5
            actions = [
                f"Inspect the readiness probe output and recent logs for {pod_name}.",
                "Confirm downstream dependencies (database, cache, DNS) are reachable.",
            ]
            commands = [
                f"kubectl -n {namespace} describe pod {pod_name}",
                f"kubectl -n {namespace} logs {pod_name} --tail=200",
            ]
        elif memory_percent >= 85.0:
            root_cause = f"Host memory pressure on the agent node ({memory_percent:.1f}% used)."
            severity = "high"
            confidence = 0.5
            actions = [
                "Identify the top memory consumers on the host.",
                "Free memory or add capacity before the kernel starts OOM killing pods.",
            ]
            commands = ["ps aux --sort=-%mem | head -n 10", "free -h"]
        elif disk_percent >= 85.0:
            root_cause = f"Host disk pressure ({disk_percent:.1f}% used on /)."
            severity = "medium"
            confidence = 0.5
            actions = ["Locate large files and prune old container images and logs."]
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

        # --- Evidence lines -------------------------------------------
        evidence_lines: list[str] = []
        for issue in issues[:5]:
            evidence_lines.append(
                f"pod {issue.get('name')} on node {issue.get('node')}: "
                f"phase={issue.get('phase')}, reason={issue.get('reason')}, "
                f"restarts={issue.get('restarts')}, exit_code={issue.get('exit_code')}"
            )
        evidence_lines.extend(node_pressure[:3])

        log_lines = [line.strip() for line in logs_text.splitlines() if line.strip()]
        interesting = [
            line
            for line in log_lines
            if any(hint in line.lower() for hint in _LOG_HINTS)
        ][-5:]
        evidence_lines.extend(f"log: {line[:200]}" for line in interesting)
        evidence_lines.extend(
            f"event {event.get('reason')}: {str(event.get('message') or '')[:160]}"
            for event in events[:4]
        )

        if zombie_count:
            evidence_lines.append(
                f"host reports {zombie_count} defunct processes "
                f"(iowait {iowait:.1f}%, pid_max {zombies.get('pid_max') or 'unknown'})"
            )
        if metrics and "error" not in metrics:
            evidence_lines.append(
                f"host CPU at {cpu_percent:.1f}%, memory at {memory_percent:.1f}% "
                f"and root disk at {disk_percent:.1f}%"
            )
        if not evidence_lines:
            evidence_lines.append(
                "No crash loop, OOM, image pull, configuration or host pressure signal observed."
            )

        summary = (
            f"{len(issues)} of {total_pods} pods in namespace '{namespace}' are "
            f"unhealthy; {zombie_count} zombie processes on the host."
        )

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
