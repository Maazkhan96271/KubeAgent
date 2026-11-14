"""Tool registry exposed to the reasoning layer.

Each collector capability is wrapped in a :class:`Tool` carrying a JSON-schema
fragment, so the same tool set can be described to an LLM or invoked directly
by the orchestrator.  Collectors return Pydantic models; the registry
serialises them to JSON-ready data before handing results back, which keeps the
LLM prompt clean and the rule engine working on plain ``dict``/``list`` values.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel

from app.collectors.k8s_collector import KubernetesCollector
from app.collectors.linux_collector import LinuxCollector

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Any]


def to_jsonable(value: Any) -> Any:
    """Recursively convert Pydantic models and datetimes into JSON-ready data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Tool:
    """A single callable capability."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def spec(self) -> dict[str, Any]:
        """OpenAI-style function description for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Name → tool mapping with async execution."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Run a blocked collector call in a worker thread and serialise it."""
        tool = self.get(name)
        arguments = arguments or {}
        logger.debug("Executing tool %s with arguments %s", name, arguments)
        result = await asyncio.to_thread(lambda: tool.handler(**arguments))
        return to_jsonable(result)


def _object_schema(
    properties: dict[str, Any], required: list[str] | None = None
) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def build_registry(k8s: KubernetesCollector, linux: LinuxCollector) -> ToolRegistry:
    """Build the default tool set from the configured collectors."""
    return ToolRegistry(
        [
            Tool(
                name="k8s.nodes",
                description=(
                    "Cluster nodes with Ready, MemoryPressure, DiskPressure and "
                    "PIDPressure conditions plus capacity."
                ),
                parameters=_object_schema({}),
                handler=k8s.get_cluster_nodes,
            ),
            Tool(
                name="k8s.unhealthy_pods",
                description=(
                    "Unhealthy pods in a namespace (CrashLoopBackOff, OOMKilled, "
                    "Pending, Error, not Ready) with restart counts and exit codes."
                ),
                parameters=_object_schema(
                    {
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace to inspect.",
                            "default": "default",
                        }
                    }
                ),
                handler=k8s.get_unhealthy_pods,
            ),
            Tool(
                name="k8s.pod_logs",
                description="Tail the stdout/stderr logs of a single pod.",
                parameters=_object_schema(
                    {
                        "namespace": {"type": "string", "default": "default"},
                        "pod_name": {
                            "type": "string",
                            "description": "Pod name to read logs from.",
                        },
                        "tail_lines": {"type": "integer", "default": 100},
                    },
                    required=["pod_name"],
                ),
                handler=k8s.get_pod_logs,
            ),
            Tool(
                name="k8s.pod_events",
                description="Warning events referencing a single pod.",
                parameters=_object_schema(
                    {
                        "namespace": {"type": "string", "default": "default"},
                        "pod_name": {
                            "type": "string",
                            "description": "Pod name to fetch events for.",
                        },
                    },
                    required=["pod_name"],
                ),
                handler=k8s.describe_pod_events,
            ),
            Tool(
                name="linux.system_metrics",
                description=(
                    "Host CPU utilisation, 1/5/15m load, iowait, memory breakdown, "
                    "disk I/O and root partition usage."
                ),
                parameters=_object_schema(
                    {"path": {"type": "string", "default": "/"}}
                ),
                handler=linux.get_system_metrics,
            ),
            Tool(
                name="linux.top_processes",
                description="Top processes by CPU or resident memory, with cmdline.",
                parameters=_object_schema(
                    {
                        "limit": {"type": "integer", "default": 5},
                        "sort_by": {
                            "type": "string",
                            "enum": ["cpu", "memory"],
                            "default": "memory",
                        },
                    }
                ),
                handler=linux.get_top_resource_processes,
            ),
            Tool(
                name="linux.zombie_processes",
                description=(
                    "Count defunct/zombie processes and report PID-table pressure."
                ),
                parameters=_object_schema({}),
                handler=linux.check_zombie_processes,
            ),
        ]
    )
