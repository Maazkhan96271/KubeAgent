"""Tool registry exposed to the reasoning layer.

Each collector capability is wrapped in a :class:`Tool` carrying a JSON-schema
fragment, so the same tool set can be described to an LLM or invoked directly
by the orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from app.collectors.k8s_collector import KubernetesCollector
from app.collectors.linux_collector import LinuxCollector

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Any]


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

    async def execute(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Run a tool's blocking collector call in a worker thread."""
        tool = self.get(name)
        arguments = arguments or {}
        logger.debug("Executing tool %s with arguments %s", name, arguments)
        return await asyncio.to_thread(lambda: tool.handler(**arguments))


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def build_registry(k8s: KubernetesCollector, linux: LinuxCollector) -> ToolRegistry:
    """Build the default tool set from the configured collectors."""
    return ToolRegistry(
        [
            Tool(
                name="k8s.list_pods",
                description=(
                    "List pods in a namespace with phase, readiness, restart count "
                    "and the current/last container termination reason."
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
                handler=k8s.list_pods,
            ),
            Tool(
                name="k8s.list_events",
                description="Recent namespace events (warnings first) with reason and message.",
                parameters=_object_schema(
                    {
                        "namespace": {
                            "type": "string",
                            "description": "Kubernetes namespace to inspect.",
                            "default": "default",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of events to return.",
                            "default": 50,
                        },
                    }
                ),
                handler=k8s.list_events,
            ),
            Tool(
                name="k8s.node_status",
                description="Cluster nodes with Ready/MemoryPressure/DiskPressure conditions.",
                parameters=_object_schema({}),
                handler=k8s.node_status,
            ),
            Tool(
                name="k8s.pod_logs",
                description="Tail the logs of a single pod container.",
                parameters=_object_schema(
                    {
                        "namespace": {"type": "string", "default": "default"},
                        "pod_name": {"type": "string", "description": "Pod name to read logs from."},
                        "tail_lines": {"type": "integer", "default": 100},
                    },
                    required=["pod_name"],
                ),
                handler=k8s.pod_logs,
            ),
            Tool(
                name="linux.host_metrics",
                description="Host CPU, memory, swap, root filesystem and load averages.",
                parameters=_object_schema({}),
                handler=linux.host_metrics,
            ),
            Tool(
                name="linux.disk_usage",
                description="Disk usage for a filesystem path.",
                parameters=_object_schema(
                    {"path": {"type": "string", "default": "/"}}
                ),
                handler=linux.disk_usage,
            ),
            Tool(
                name="linux.top_processes",
                description="Top processes by memory usage on the host.",
                parameters=_object_schema({"limit": {"type": "integer", "default": 5}}),
                handler=linux.top_processes,
            ),
        ]
    )
