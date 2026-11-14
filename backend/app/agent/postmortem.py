"""Automated SRE post-mortem and root-cause analysis reports.

The generator is deliberately framework-free: it takes plain dictionaries (the
JSON form of an incident and its event history) and returns GitHub-Flavored
Markdown plus an optional standalone HTML rendering.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

try:  # optional: only needed for the rendered HTML view
    import markdown as _markdown
except ImportError:  # pragma: no cover - requirements.txt pins markdown
    _markdown = None

#: Namespace for deterministic report UUIDs (one stable UUID per incident).
REPORT_NAMESPACE = uuid.UUID("3f1c9f6e-6f0b-4f3f-9c62-6f4f1f2b8a10")

#: Human-readable phase labels for stored event types.
_EVENT_PHASES: dict[str, str] = {
    "snapshot": "Stream attached",
    "incident.created": "Alert triggered",
    "status": "Status transition",
    "agent.thought": "Agent inspection",
    "agent.observation": "Evidence collected",
    "agent.verification": "Root cause identified",
    "diagnosis.completed": "Diagnosis complete",
    "approval.granted": "Remediation approved",
    "error": "Agent error",
    "ping": "Heartbeat",
}


class PostMortemReport(BaseModel):
    """A rendered post-mortem for one incident."""

    report_id: str
    incident_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    mttr_seconds: float | None = None
    markdown: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _cell(value: Any) -> str:
    """Escape a value for a Markdown table cell."""
    text = str(value if value is not None else "—")
    return text.replace("|", "\\|").replace("\n", " ").strip() or "—"


def _format_timestamp(value: Any) -> str:
    parsed = _parse_ts(value)
    return parsed.astimezone(timezone.utc).strftime("%H:%M:%S") if parsed else "—"


def _format_mttr(seconds: float | None) -> str:
    if seconds is None:
        return "not resolved yet"
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minutes"
    return f"{seconds / 3600:.2f} hours"


def _phase_for(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "event")
    if event_type == "status":
        status = str((event.get("data") or {}).get("status") or "").replace("_", " ").title()
        return f"Status → {status}" if status else "Status transition"
    return _EVENT_PHASES.get(event_type, event_type)


def _detail_for(event: dict[str, Any]) -> str:
    message = str(event.get("message") or "").strip()
    tool = (event.get("data") or {}).get("tool")
    if tool and tool not in message:
        return f"{message} ({tool})" if message else str(tool)
    return message or "—"


def _compute_mttr(incident: dict[str, Any], events: list[dict[str, Any]]) -> float | None:
    created = _parse_ts(incident.get("created_at"))
    if created is None:
        return None

    resolved_at: datetime | None = None
    for event in events:
        if (
            event.get("type") == "status"
            and (event.get("data") or {}).get("status") == "RESOLVED"
        ):
            resolved_at = _parse_ts(event.get("created_at"))
    if resolved_at is None:
        approval = incident.get("approval") or {}
        resolved_at = _parse_ts(approval.get("approved_at"))
    if resolved_at is None and incident.get("status") == "RESOLVED":
        resolved_at = _parse_ts(incident.get("updated_at"))
    if resolved_at is None:
        return None
    return max((resolved_at - created).total_seconds(), 0.0)


def _recommendations(
    incident: dict[str, Any], diagnosis: dict[str, Any]
) -> list[tuple[str, str]]:
    workload = str(incident.get("workload") or f"{incident.get('namespace')} workload")
    namespace = str(incident.get("namespace") or "default")
    # Only the root cause and proposed commands drive the advice. The summary
    # and evidence carry baseline host metrics ("0 zombie processes", host CPU
    # percentages) that would otherwise trigger unrelated recommendations.
    haystack = " ".join(
        [
            str(diagnosis.get("root_cause") or ""),
            " ".join(str(item) for item in diagnosis.get("commands") or []),
        ]
    ).lower()

    recommendations: list[tuple[str, str]] = []

    if any(token in haystack for token in ("oom", "out of memory", "exit code 137", "memory limit")):
        recommendations.append(
            (
                f"Raise the memory limit for {workload} to 1Gi (requests 512Mi) or remove "
                "the allocation leak, then re-run a load test.",
                "The container was killed at its memory ceiling; headroom prevents repeat OOMKills.",
            )
        )
        recommendations.append(
            (
                f"Add a memory-based HPA and alert on container_memory_working_set_bytes "
                f"> 80% of limit in namespace {namespace}.",
                "Autoscaling plus early warning stops one pod from exhausting its node.",
            )
        )
    if any(token in haystack for token in ("secret", "password", "db_password", "config error", "missing")):
        recommendations.append(
            (
                "Manage credentials with External Secrets Operator or SealedSecrets, and add "
                "a startup probe that validates required environment variables.",
                "Missing secret keys should fail fast and be auditable instead of crash looping.",
            )
        )
    if any(token in haystack for token in ("defunct", "zombie", "pid", "eagain")):
        recommendations.append(
            (
                "Reap child processes in the worker (SIGCHLD handler or subprocess.wait()) and "
                "alert on process-count growth.",
                "Unreaped children exhaust the PID table and break fork().",
            )
        )
    if any(token in haystack for token in ("cpu", "throttl", "load average")):
        recommendations.append(
            (
                f"Add a CPU-based HPA for {workload} and raise its CPU limit to stop throttling.",
                "Sustained throttling triggers probe timeouts and latency SLO burn.",
            )
        )
    if "imagepull" in haystack or "errimagepull" in haystack:
        recommendations.append(
            (
                "Pin image digests and verify imagePullSecrets in CI before each rollout.",
                "Image pull failures are then caught before they reach production.",
            )
        )
    if "pending" in haystack or "unschedul" in haystack:
        recommendations.append(
            (
                "Review resource requests and enable cluster autoscaling for constrained nodes.",
                "Unschedulable pods point at capacity or taint/toleration mismatches.",
            )
        )

    recommendations.append(
        (
            f"Add a Prometheus alert rule and runbook entry for this failure signature in {namespace}.",
            "Automated detection shortens MTTR when the failure repeats.",
        )
    )
    if not diagnosis.get("commands"):
        recommendations.append(
            (
                "Capture a diagnostic bundle (logs, events, metrics) automatically on detection.",
                "Evidence collected at failure time speeds up root cause analysis.",
            )
        )
    return recommendations


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------
def generate_postmortem(
    incident: dict[str, Any], events: list[dict[str, Any]]
) -> PostMortemReport:
    """Build a Markdown post-mortem for an incident and its event history."""
    incident_id = str(incident.get("id") or "unknown")
    report_id = str(uuid.uuid5(REPORT_NAMESPACE, f"kubeagent/postmortem/{incident_id}"))
    diagnosis = incident.get("diagnosis") or {}
    approval = incident.get("approval") or {}
    mttr_seconds = _compute_mttr(incident, events)

    title = str(incident.get("title") or "Untitled incident")
    status = str(incident.get("status") or "OPEN")
    severity = str(incident.get("severity") or "medium").upper()
    namespace = str(incident.get("namespace") or "default")
    workload = str(incident.get("workload") or "—")
    source = str(incident.get("source") or "—")
    summary = str(diagnosis.get("summary") or incident.get("description") or "No summary captured.")
    root_cause = str(diagnosis.get("root_cause") or "The agent did not record a root cause.")
    confidence = diagnosis.get("confidence")
    confidence_text = f"{float(confidence) * 100:.0f}%" if isinstance(confidence, (int, float)) else "—"

    resolved_at = approval.get("approved_at") if approval else None
    if not resolved_at and status == "RESOLVED":
        resolved_at = incident.get("updated_at")

    lines: list[str] = [
        "> [!NOTE]",
        "> Automated post-mortem generated by **KubeAgent**. Figures are derived from the",
        "> incident event log and the agent's recorded diagnosis.",
        "",
        f"# Post-Mortem: {title}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Report UUID | `{report_id}` |",
        f"| Incident ID | `{incident_id}` |",
        f"| Severity | **{severity}** |",
        f"| Status | {status.replace('_', ' ')} |",
        f"| Namespace | `{_cell(namespace)}` |",
        f"| Workload | `{_cell(workload)}` |",
        f"| Source | {_cell(source)} |",
        f"| Detected (UTC) | {_format_timestamp(incident.get('created_at'))} |",
        f"| Resolved (UTC) | {_format_timestamp(resolved_at)} |",
        f"| **MTTR** | **{_format_mttr(mttr_seconds)}** |",
        f"| Diagnosis confidence | {confidence_text} |",
        "",
        "## Summary",
        "",
        summary,
        "",
        "> [!IMPORTANT]",
        f"> **Root cause:** {root_cause}",
        "",
        "## Timeline of Events",
        "",
        "| Time (UTC) | Phase | Detail |",
        "| --- | --- | --- |",
    ]

    if events:
        for event in events:
            lines.append(
                f"| {_format_timestamp(event.get('created_at'))} | {_cell(_phase_for(event))} "
                f"| {_cell(_detail_for(event))} |"
            )
    else:
        lines.append("| — | — | No events were recorded for this incident. |")

    lines += [
        "",
        "## Root Cause Analysis",
        "",
        root_cause,
        "",
    ]

    evidence = [str(item) for item in diagnosis.get("evidence") or []]
    if evidence:
        lines += ["### Evidence from logs and telemetry", ""]
        lines += [f"- {item}" for item in evidence]
        lines.append("")

    lines += [
        "## Remediation Executed",
        "",
    ]

    commands = [str(item) for item in diagnosis.get("commands") or []]
    if commands:
        approved = bool(approval)
        lines += [
            "| # | Action | State |",
            "| --- | --- | --- |",
        ]
        for index, command in enumerate(commands, start=1):
            state = "Approved by operator" if approved else "Proposed — awaiting approval"
            lines.append(f"| {index} | `{_cell(command)}` | {state} |")
        lines.append("")
    else:
        lines += ["No remediation commands were proposed for this incident.", ""]

    if approval:
        lines += [
            "> [!TIP]",
            f"> Approved by **{_cell(approval.get('approved_by'))}** at "
            f"{_format_timestamp(approval.get('approved_at'))} UTC"
            + (f" — notes: {_cell(approval.get('notes'))}" if approval.get("notes") else ""),
            "",
        ]

    lines += [
        "## Preventive Recommendations",
        "",
        "| # | Recommendation | Rationale |",
        "| --- | --- | --- |",
    ]
    for index, (recommendation, rationale) in enumerate(
        _recommendations(incident, diagnosis), start=1
    ):
        lines.append(f"| {index} | {_cell(recommendation)} | {_cell(rationale)} |")

    lines += [
        "",
        "---",
        "",
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC by KubeAgent "
        f"for `{incident_id}`._",
        "",
    ]

    report = PostMortemReport(
        report_id=report_id,
        incident_id=incident_id,
        mttr_seconds=mttr_seconds,
        markdown="\n".join(lines),
    )
    logger.debug("Generated post-mortem %s for %s", report_id, incident_id)
    return report


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Post-Mortem · {title}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; padding: 2.5rem 1.5rem;
    background: #020617; color: #cbd5e1;
    font: 15px/1.65 ui-monospace, SFMono-Regular, Menlo, monospace;
  }}
  main {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ color: #f8fafc; font-size: 1.6rem; border-bottom: 1px solid #1e293b; padding-bottom: .6rem; }}
  h2 {{ color: #6ee7b7; font-size: 1.15rem; margin-top: 2rem; }}
  h3 {{ color: #a7f3d0; font-size: 1rem; }}
  a {{ color: #38bdf8; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 13px; }}
  th, td {{ border: 1px solid #1e293b; padding: .55rem .7rem; text-align: left; vertical-align: top; }}
  th {{ background: #0b1220; color: #94a3b8; text-transform: uppercase; letter-spacing: .06em; font-size: 11px; }}
  tr:nth-child(even) td {{ background: rgba(15, 23, 42, .45); }}
  code {{ background: #0b1220; border: 1px solid #1e293b; border-radius: 4px; padding: .1rem .35rem; color: #6ee7b7; }}
  pre {{ background: #0b1220; border: 1px solid #1e293b; border-radius: 8px; padding: 1rem; overflow-x: auto; }}
  blockquote {{
    margin: 1rem 0; padding: .85rem 1rem;
    border-left: 3px solid #34d399; background: rgba(16, 185, 129, .07);
    border-radius: 0 8px 8px 0;
  }}
  blockquote p {{ margin: 0; color: #a7f3d0; }}
  hr {{ border: none; border-top: 1px solid #1e293b; margin: 2rem 0 1rem; }}
  em {{ color: #64748b; }}
</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def render_html(report: PostMortemReport) -> str:
    """Render the report as a standalone, dark-themed HTML document."""
    if _markdown is not None:
        body = _markdown.markdown(
            report.markdown,
            extensions=["tables", "fenced_code", "sane_lists"],
            output_format="html5",
        )
    else:  # pragma: no cover - fallback when the markdown package is absent
        body = f"<pre>{html.escape(report.markdown)}</pre>"
    title = html.escape(report.incident_id)
    return _HTML_TEMPLATE.format(title=title, body=body)
