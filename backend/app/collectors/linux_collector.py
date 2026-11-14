"""Host-level Linux metrics collected with psutil.

These readings describe the machine running KubeAgent (its container or node),
which is useful context when a pod symptom is actually host resource pressure.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import psutil

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


def _hostname() -> str:
    try:
        return os.uname().nodename
    except AttributeError:  # pragma: no cover - non-POSIX hosts
        return os.environ.get("HOSTNAME", "unknown")


def _load_average() -> dict[str, float | None]:
    try:
        load_1, load_5, load_15 = os.getloadavg()
    except (AttributeError, OSError):  # pragma: no cover - non-POSIX hosts
        return {"load_1m": None, "load_5m": None, "load_15m": None}
    return {"load_1m": load_1, "load_5m": load_5, "load_15m": load_15}


class LinuxCollector:
    """Read-only host metrics used as agent tools."""

    def host_metrics(self) -> dict[str, Any]:
        """CPU, memory, swap, disk and load for the host running the agent."""
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        try:
            disk = psutil.disk_usage("/")
            disk_payload: dict[str, Any] = {
                "path": "/",
                "percent": disk.percent,
                "total_mb": round(disk.total / _MB, 1),
                "free_mb": round(disk.free / _MB, 1),
            }
        except OSError as exc:
            logger.debug("disk usage unavailable: %s", exc)
            disk_payload = {"path": "/", "error": str(exc)}

        boot_time = psutil.boot_time()
        return {
            "hostname": _hostname(),
            "cpu": {
                "percent": psutil.cpu_percent(interval=0.2),
                "logical_count": psutil.cpu_count(logical=True),
                "physical_count": psutil.cpu_count(logical=False),
                **_load_average(),
            },
            "memory": {
                "percent": memory.percent,
                "total_mb": round(memory.total / _MB, 1),
                "available_mb": round(memory.available / _MB, 1),
            },
            "swap": {
                "percent": swap.percent,
                "total_mb": round(swap.total / _MB, 1),
                "used_mb": round(swap.used / _MB, 1),
            },
            "disk_root": disk_payload,
            "boot_time": boot_time,
            "uptime_seconds": round(max(time.time() - boot_time, 0.0), 1),
        }

    def disk_usage(self, path: str = "/") -> dict[str, Any]:
        """Disk usage for a single filesystem path."""
        try:
            usage = psutil.disk_usage(path)
        except OSError as exc:
            raise ValueError(f"cannot read disk usage for '{path}': {exc}") from exc
        return {
            "path": path,
            "percent": usage.percent,
            "total_mb": round(usage.total / _MB, 1),
            "used_mb": round(usage.used / _MB, 1),
            "free_mb": round(usage.free / _MB, 1),
        }

    def top_processes(self, limit: int = 5) -> list[dict[str, Any]]:
        """Top processes by resident memory, descending."""
        limit = max(int(limit), 1)
        processes: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
            info = proc.info
            processes.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name"),
                    "memory_percent": round(float(info.get("memory_percent") or 0.0), 2),
                    "cpu_percent": round(float(info.get("cpu_percent") or 0.0), 2),
                }
            )
        processes.sort(key=lambda item: item["memory_percent"], reverse=True)
        return processes[:limit]
