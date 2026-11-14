"""Linux host telemetry collected with psutil and direct ``/proc`` inspection.

Everything here is read-only.  Metric collection is best-effort: a failing
subsystem (no ``/proc``, a restricted container, no disk counters) is recorded
in ``SystemMetrics.warnings`` instead of raising, so the agent can still reason
about whatever *was* observable.
"""

from __future__ import annotations

import logging
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
_GB = 1024 * _MB

_PROC_STAT = Path("/proc/stat")
_PROC_MEMINFO = Path("/proc/meminfo")
_PROC_PID_MAX = Path("/proc/sys/kernel/pid_max")

#: A process usage above this share of ``pid_max`` is treated as PID pressure.
PID_PRESSURE_THRESHOLD_PERCENT = 75.0


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class LoadAverage(BaseModel):
    """1/5/15 minute load averages, ``None`` where the host does not report them."""

    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None
    cpu_count: int | None = None

    @property
    def normalized_load_1m(self) -> float | None:
        """Load normalised by CPU count, the usual saturation signal."""
        if self.load_1m is None or not self.cpu_count:
            return None
        return round(self.load_1m / self.cpu_count, 3)


class CpuMetrics(BaseModel):
    """CPU utilisation plus the load average that explains queueing."""

    percent: float = Field(description="CPU utilisation sampled over ~200ms, 0-100.")
    logical_count: int | None = None
    physical_count: int | None = None
    load: LoadAverage
    iowait_percent: float | None = Field(
        default=None,
        description="Share of CPU time spent waiting on I/O during the sample window.",
    )


class MemoryBreakdown(BaseModel):
    """Memory usage with the reclaimable/anon breakdown from ``/proc/meminfo``."""

    total_mb: float = 0.0
    available_mb: float = 0.0
    used_mb: float = 0.0
    free_mb: float = 0.0
    percent: float = 0.0
    cached_mb: float | None = None
    buffers_mb: float | None = None
    active_mb: float | None = None
    inactive_mb: float | None = None
    dirty_mb: float | None = None
    swap_total_mb: float = 0.0
    swap_used_mb: float = 0.0
    swap_percent: float = 0.0


class DiskUsage(BaseModel):
    """Capacity and inode usage for one filesystem path."""

    path: str = "/"
    percent: float = 0.0
    total_gb: float = 0.0
    used_gb: float = 0.0
    free_gb: float = 0.0
    inodes_percent: float | None = None


class DiskIoMetrics(BaseModel):
    """Cumulative disk I/O counters plus the rate measured over the sample window."""

    read_mb: float = 0.0
    write_mb: float = 0.0
    read_count: int = 0
    write_count: int = 0
    read_time_ms: int = 0
    write_time_ms: int = 0
    interval_seconds: float | None = None
    read_mb_per_s: float | None = None
    write_mb_per_s: float | None = None


class SystemMetrics(BaseModel):
    """Full host snapshot returned by :meth:`LinuxCollector.get_system_metrics`."""

    hostname: str
    kernel: str | None = None
    platform: str | None = None
    boot_time: datetime | None = None
    uptime_seconds: float = 0.0
    cpu: CpuMetrics
    memory: MemoryBreakdown
    disk_io: DiskIoMetrics | None = None
    root_disk: DiskUsage | None = None
    process_count: int = 0
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: list[str] = Field(default_factory=list)


class ProcessUsage(BaseModel):
    """One process row for the top-consumer report."""

    pid: int
    name: str
    cmdline: str = ""
    username: str | None = None
    status: str | None = None
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    rss_mb: float = 0.0


class ZombieProcess(BaseModel):
    """A single defunct process entry."""

    pid: int
    ppid: int | None = None
    name: str
    status: str = "zombie"


class ZombieReport(BaseModel):
    """Defunct-process census, including PID-table pressure."""

    zombie_count: int = 0
    pids: list[int] = Field(default_factory=list)
    samples: list[ZombieProcess] = Field(default_factory=list)
    total_processes: int = 0
    pid_max: int | None = None
    pid_usage_percent: float | None = None
    pid_pressure: bool = False
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------
def _hostname() -> str:
    try:
        return os.uname().nodename
    except AttributeError:  # pragma: no cover - non-POSIX hosts
        return os.environ.get("HOSTNAME", "unknown")


def _load_average(cpu_count: int | None) -> LoadAverage:
    try:
        load_1, load_5, load_15 = os.getloadavg()
    except (AttributeError, OSError):  # pragma: no cover - non-POSIX hosts
        return LoadAverage(cpu_count=cpu_count)
    return LoadAverage(
        load_1m=round(load_1, 3),
        load_5m=round(load_5, 3),
        load_15m=round(load_15, 3),
        cpu_count=cpu_count,
    )


def _read_cpu_times() -> tuple[float, float] | None:
    """Return ``(iowait_jiffies, total_jiffies)`` from the aggregate ``cpu`` line."""
    try:
        with _PROC_STAT.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("cpu "):
                    continue
                fields = [float(value) for value in line.split()[1:]]
                if len(fields) < 5:
                    return None
                return fields[4], sum(fields)
    except (OSError, ValueError) as exc:
        logger.debug("cannot read /proc/stat: %s", exc)
    return None


def _iowait_percent(sample_seconds: float = 0.1) -> float | None:
    """CPU I/O wait share measured over a short delta (not since boot)."""
    first = _read_cpu_times()
    if first is None:
        return None
    time.sleep(sample_seconds)
    second = _read_cpu_times()
    if second is None:
        return None
    delta_iowait = second[0] - first[0]
    delta_total = second[1] - first[1]
    if delta_total <= 0:
        return None
    return round(max(delta_iowait, 0.0) / delta_total * 100.0, 2)


def _read_meminfo() -> dict[str, float]:
    """Parse ``/proc/meminfo`` into a ``{key: megabytes}`` mapping."""
    values: dict[str, float] = {}
    try:
        content = _PROC_MEMINFO.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("cannot read /proc/meminfo: %s", exc)
        return values
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            values[key.strip()] = round(float(parts[0]) / 1024.0, 1)
        except ValueError:
            continue
    return values


def _pid_max() -> int | None:
    try:
        return int(_PROC_PID_MAX.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        logger.debug("cannot read kernel pid_max: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Metric builders
# ---------------------------------------------------------------------------
def _memory_breakdown(meminfo: dict[str, float], warnings: list[str]) -> MemoryBreakdown:
    try:
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
    except Exception as exc:  # pragma: no cover - platform dependent
        warnings.append(f"memory metrics unavailable: {exc}")
        return MemoryBreakdown()

    cached = meminfo.get("Cached", getattr(virtual, "cached", None))
    buffers = meminfo.get("Buffers", getattr(virtual, "buffers", None))
    return MemoryBreakdown(
        total_mb=round(virtual.total / _MB, 1),
        available_mb=round(virtual.available / _MB, 1),
        used_mb=round((virtual.total - virtual.available) / _MB, 1),
        free_mb=round(virtual.free / _MB, 1),
        percent=round(float(virtual.percent), 2),
        cached_mb=cached,
        buffers_mb=buffers,
        active_mb=meminfo.get("Active"),
        inactive_mb=meminfo.get("Inactive"),
        dirty_mb=meminfo.get("Dirty"),
        swap_total_mb=round(swap.total / _MB, 1),
        swap_used_mb=round(swap.used / _MB, 1),
        swap_percent=round(float(swap.percent), 2),
    )


def _disk_usage(path: str, warnings: list[str]) -> DiskUsage | None:
    try:
        usage = psutil.disk_usage(path)
    except (OSError, ValueError) as exc:
        warnings.append(f"disk usage for '{path}' unavailable: {exc}")
        return None

    inodes_percent: float | None = None
    try:
        statvfs = os.statvfs(path)
        if statvfs.f_files:
            inodes_percent = round(
                (statvfs.f_files - statvfs.f_ffree) / statvfs.f_files * 100.0, 2
            )
    except (AttributeError, OSError):
        inodes_percent = None

    return DiskUsage(
        path=path,
        percent=round(float(usage.percent), 2),
        total_gb=round(usage.total / _GB, 2),
        used_gb=round(usage.used / _GB, 2),
        free_gb=round(usage.free / _GB, 2),
        inodes_percent=inodes_percent,
    )


def _disk_io_metrics(
    warnings: list[str], sample_seconds: float = 0.2
) -> DiskIoMetrics | None:
    try:
        first = psutil.disk_io_counters()
    except Exception as exc:  # pragma: no cover - platform dependent
        warnings.append(f"disk I/O counters unavailable: {exc}")
        return None
    if first is None:
        warnings.append("disk I/O counters are not exposed by this host")
        return None

    time.sleep(sample_seconds)
    try:
        second = psutil.disk_io_counters()
    except Exception as exc:  # pragma: no cover - platform dependent
        warnings.append(f"disk I/O counters unavailable: {exc}")
        return None
    if second is None:
        warnings.append("disk I/O counters disappeared during sampling")
        return None

    elapsed = max(sample_seconds, 1e-6)
    return DiskIoMetrics(
        read_mb=round(second.read_bytes / _MB, 1),
        write_mb=round(second.write_bytes / _MB, 1),
        read_count=int(second.read_count),
        write_count=int(second.write_count),
        read_time_ms=int(second.read_time),
        write_time_ms=int(second.write_time),
        interval_seconds=round(elapsed, 3),
        read_mb_per_s=round(max(second.read_bytes - first.read_bytes, 0) / _MB / elapsed, 2),
        write_mb_per_s=round(
            max(second.write_bytes - first.write_bytes, 0) / _MB / elapsed, 2
        ),
    )


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
class LinuxCollector:
    """Read-only Linux host telemetry used as agent tools."""

    def get_system_metrics(self, path: str = "/") -> SystemMetrics:
        """CPU load, memory breakdown, disk I/O and root partition usage."""
        warnings: list[str] = []

        try:
            cpu_percent = round(float(psutil.cpu_percent(interval=0.2)), 2)
        except Exception as exc:  # pragma: no cover - platform dependent
            warnings.append(f"CPU utilisation unavailable: {exc}")
            cpu_percent = 0.0

        logical = psutil.cpu_count(logical=True)
        physical = psutil.cpu_count(logical=False)
        cpu = CpuMetrics(
            percent=cpu_percent,
            logical_count=logical,
            physical_count=physical,
            load=_load_average(logical),
            iowait_percent=_iowait_percent(),
        )

        memory = _memory_breakdown(_read_meminfo(), warnings)
        disk_io = _disk_io_metrics(warnings)
        root_disk = _disk_usage(path, warnings)

        boot_epoch: float | None = None
        try:
            boot_epoch = psutil.boot_time()
        except Exception as exc:  # pragma: no cover - platform dependent
            warnings.append(f"boot time unavailable: {exc}")

        try:
            process_count = len(psutil.pids())
        except Exception as exc:  # pragma: no cover - platform dependent
            warnings.append(f"process census unavailable: {exc}")
            process_count = 0

        return SystemMetrics(
            hostname=_hostname(),
            kernel=platform.release() or None,
            platform=platform.platform() or None,
            boot_time=datetime.fromtimestamp(boot_epoch, tz=timezone.utc)
            if boot_epoch
            else None,
            uptime_seconds=round(max(time.time() - boot_epoch, 0.0), 1)
            if boot_epoch
            else 0.0,
            cpu=cpu,
            memory=memory,
            disk_io=disk_io,
            root_disk=root_disk,
            process_count=process_count,
            warnings=warnings,
        )

    def get_top_resource_processes(
        self,
        limit: int = 5,
        sort_by: Literal["cpu", "memory"] = "memory",
    ) -> list[ProcessUsage]:
        """Top processes by CPU or resident memory, with PID, name and cmdline.

        ``cpu_percent`` is measured since the previous collection call for the
        same process, so the list is primed once before ranking.
        """
        limit = max(int(limit), 1)

        for proc in psutil.process_iter(["pid"]):
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(0.1)

        rows: list[ProcessUsage] = []
        for proc in psutil.process_iter(
            ["pid", "name", "username", "status", "cpu_percent", "memory_percent"]
        ):
            info = proc.info
            try:
                cmdline = " ".join(proc.cmdline())[:512]
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                cmdline = ""
            try:
                rss_mb = round(proc.memory_info().rss / _MB, 2)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                rss_mb = 0.0

            rows.append(
                ProcessUsage(
                    pid=int(info.get("pid") or 0),
                    name=str(info.get("name") or "unknown"),
                    cmdline=cmdline,
                    username=info.get("username"),
                    status=info.get("status"),
                    cpu_percent=round(float(info.get("cpu_percent") or 0.0), 2),
                    memory_percent=round(float(info.get("memory_percent") or 0.0), 2),
                    rss_mb=rss_mb,
                )
            )

        key = "cpu_percent" if sort_by == "cpu" else "memory_percent"
        rows.sort(key=lambda row: getattr(row, key), reverse=True)
        return rows[:limit]

    def check_zombie_processes(self, sample_limit: int = 20) -> ZombieReport:
        """Count defunct processes and report PID-table pressure."""
        warnings: list[str] = []
        zombies: list[ZombieProcess] = []

        try:
            total_processes = len(psutil.pids())
        except Exception as exc:  # pragma: no cover - platform dependent
            warnings.append(f"process census unavailable: {exc}")
            total_processes = 0

        for proc in psutil.process_iter(["pid", "ppid", "name", "status"]):
            try:
                info = proc.info
                status = str(info.get("status") or "")
                if status != psutil.STATUS_ZOMBIE and status.lower() != "zombie":
                    continue
                zombies.append(
                    ZombieProcess(
                        pid=int(info.get("pid") or 0),
                        ppid=int(info["ppid"]) if info.get("ppid") is not None else None,
                        name=str(info.get("name") or "defunct"),
                        status=status or "zombie",
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        pid_max = _pid_max()
        usage_percent: float | None = None
        if pid_max:
            usage_percent = round(total_processes / pid_max * 100.0, 3)

        return ZombieReport(
            zombie_count=len(zombies),
            pids=[zombie.pid for zombie in zombies][:500],
            samples=sorted(zombies, key=lambda item: item.pid)[: max(sample_limit, 1)],
            total_processes=total_processes,
            pid_max=pid_max,
            pid_usage_percent=usage_percent,
            pid_pressure=bool(usage_percent and usage_percent >= PID_PRESSURE_THRESHOLD_PERCENT),
            warnings=warnings,
        )
