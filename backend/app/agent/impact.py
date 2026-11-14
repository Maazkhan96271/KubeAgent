"""Declarative remediation patches and blast-radius assessment.

Both helpers turn an incident's diagnosis into facts an operator can inspect
before approving: a before/after resource diff and an impact assessment with a
rollback plan.  Nothing here executes anything.
"""

from __future__ import annotations

import re
from typing import Any

_MEMORY_LIMIT = re.compile(
    r"memory limit\s+(\d+(?:\.\d+)?\s?(?:Mi|Gi|MB|GB))", re.IGNORECASE
)
_SET_RESOURCES = re.compile(r"set resources deployment/([a-z0-9][a-z0-9-]*)", re.IGNORECASE)
_SCALE = re.compile(r"scale deployment/([a-z0-9][a-z0-9-]*)", re.IGNORECASE)


def _commands(diagnosis: dict[str, Any] | None) -> list[str]:
    return [str(command) for command in (diagnosis or {}).get("commands") or []]


def _normalise_deployment(name: str) -> str:
    """Strip a ReplicaSet/pod suffix (``api-6f8b9c7d4-4kq2p`` → ``api``)."""
    parts = name.rsplit("-", 2)
    if len(parts) == 3 and not parts[2].isdigit():
        return parts[0]
    return name


def _deployment(incident: dict[str, Any], commands: list[str]) -> str | None:
    for command in commands:
        for pattern in (_SET_RESOURCES, _SCALE):
            match = pattern.search(command)
            if match:
                return _normalise_deployment(match.group(1))
    workload = str(incident.get("workload") or "").strip()
    return _normalise_deployment(workload) if workload else None


def _current_limit(incident: dict[str, Any], diagnosis: dict[str, Any]) -> str | None:
    parts = [str(item) for item in diagnosis.get("evidence") or []]
    parts.append(str(incident.get("description") or ""))
    match = _MEMORY_LIMIT.search(" ".join(parts))
    return match.group(1).replace(" ", "") if match else None


def build_patch(
    incident: dict[str, Any], diagnosis: dict[str, Any] | None
) -> dict[str, Any]:
    """Return a unified, before/after patch preview for one incident."""
    commands = _commands(diagnosis)
    deployment = _deployment(incident, commands)
    namespace = str(incident.get("namespace") or "default")
    set_command = next((command for command in commands if "set resources" in command), None)
    scale_command = next((command for command in commands if "scale deployment" in command), None)

    before_limit = _current_limit(incident, diagnosis or {})
    after_limit = after_request = replicas = None

    if set_command:
        limits = (re.search(r"--limits=(\S+)", set_command) or [None, ""])[1]
        requests = (re.search(r"--requests=(\S+)", set_command) or [None, ""])[1]
        after_limit = (re.search(r"memory=(\S+)", limits) or [None, None])[1]
        after_request = (re.search(r"memory=(\S+)", requests) or [None, None])[1]
    if scale_command:
        replicas = (re.search(r"--replicas=(\d+)", scale_command) or [None, None])[1]

    changes: list[dict[str, str | None]] = []
    if after_limit:
        changes.append(
            {
                "path": "resources.limits.memory",
                "before": before_limit,
                "after": after_limit,
            }
        )
    if after_request:
        changes.append(
            {"path": "resources.requests.memory", "before": None, "after": after_request}
        )
    if replicas:
        changes.append({"path": "spec.replicas", "before": None, "after": replicas})

    if not changes or not deployment:
        return {
            "available": False,
            "target": f"deployment/{deployment}" if deployment else None,
            "namespace": namespace,
            "changes": [],
            "diff": [],
            "note": "No declarative resource change could be derived from this diagnosis; review the command list.",
        }

    lines: list[dict[str, str]] = [
        {"type": "context", "text": "spec:"},
        {"type": "context", "text": "  template:"},
        {"type": "context", "text": "    spec:"},
        {"type": "context", "text": "      containers:"},
        {"type": "context", "text": f"      - name: {deployment}"},
        {"type": "context", "text": "        resources:"},
    ]
    if after_limit:
        lines.append({"type": "context", "text": "          limits:"})
        if before_limit:
            lines.append({"type": "del", "text": f'-           memory: "{before_limit}"'})
        lines.append({"type": "add", "text": f'+           memory: "{after_limit}"'})
    if after_request:
        lines.append({"type": "context", "text": "          requests:"})
        lines.append({"type": "add", "text": f'+           memory: "{after_request}"'})
    if replicas:
        lines = [
            {"type": "context", "text": "spec:"},
            {"type": "add", "text": f"+  replicas: {replicas}"},
        ]

    return {
        "available": True,
        "target": f"deployment/{deployment}",
        "namespace": namespace,
        "changes": changes,
        "diff": lines,
        "note": (
            None
            if before_limit or replicas
            else "Current limit not present in the collected evidence; only the proposed value is shown."
        ),
    }


def build_impact(
    incident: dict[str, Any],
    diagnosis: dict[str, Any] | None,
    pods_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess risk for the proposed remediation and name a rollback plan."""
    commands = _commands(diagnosis)
    deployment = _deployment(incident, commands)
    namespace = str(incident.get("namespace") or "default")

    issues = [item for item in (pods_report or {}).get("issues") or [] if isinstance(item, dict)]
    target_issues = [
        item
        for item in issues
        if deployment and str(item.get("name") or "").startswith(deployment)
    ]
    namespace_pods = (pods_report or {}).get("total_pods")

    risk = "LOW"
    rationale: list[str] = []
    if any(re.search(r"scale .*--replicas=0\b", command) for command in commands):
        risk = "HIGH"
        rationale.append("A scale-down to zero replicas removes all serving capacity.")
    elif any("delete pod" in command for command in commands):
        risk = "MEDIUM"
        rationale.append("Deleting a pod interrupts in-flight requests until it is rescheduled.")
    elif target_issues:
        rationale.append(
            "The target pod is already unhealthy, so a rolling restart cannot reduce availability further."
        )
    if any("rollout restart" in command for command in commands) and risk == "LOW":
        rationale.append("Rolling restart keeps the remaining replicas serving traffic.")

    observed = {
        "target_pods": len(target_issues),
        "unhealthy_target_pods": len(target_issues),
        "namespace_pods": namespace_pods,
        "names": [str(item.get("name")) for item in target_issues][:3],
    }
    if namespace_pods is None and not target_issues:
        rationale.append("Live replica topology was unavailable; risk is based on the action type only.")

    return {
        "target": f"deployment/{deployment}" if deployment else (incident.get("workload") or None),
        "namespace": namespace,
        "risk": risk,
        "rationale": rationale
        or ["No destructive verb detected; the proposal is limited to a declarative resource change."],
        "observed": observed,
        "rollback": (
            f"kubectl -n {namespace} rollout undo deployment/{deployment}" if deployment else None
        ),
    }
