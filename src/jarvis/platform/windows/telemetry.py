"""System telemetry: CPU, RAM, disks, GPU and processes.

CPU, memory, disk and process data come from ``psutil``. On Windows, process
memory comes from one system-wide process snapshot instead of one query per
process: a few protected services make the per-process query take hundreds of
milliseconds each. GPU data comes from ``nvidia-smi`` (the same approach as
``jarvis.utils.vram``), so it is reported only for NVIDIA GPUs and omitted when
the tool is missing. Only process names are read; command lines and window
titles are never collected.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    import psutil
except ImportError:  # pragma: no cover - psutil ships with the desktop app
    psutil = None  # type: ignore[assignment]

from ...debug import debug_log

# Window over which CPU utilisation is measured.
CPU_SAMPLE_SEC = 0.3
GPU_QUERY_TIMEOUT_SEC = 4.0

_GPU_QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
_RANKINGS = ("memory", "cpu")


@dataclass(frozen=True)
class CpuUsage:
    percent: float
    logical_cores: int
    physical_cores: Optional[int]


@dataclass(frozen=True)
class MemoryUsage:
    used_bytes: int
    total_bytes: int
    percent: float


@dataclass(frozen=True)
class DiskUsage:
    mount: str
    used_bytes: int
    total_bytes: int
    percent: float


@dataclass(frozen=True)
class GpuStats:
    name: str
    utilization_percent: Optional[int]
    vram_used_mb: Optional[int]
    vram_total_mb: Optional[int]
    temperature_c: Optional[int]


@dataclass(frozen=True)
class ProcessGroup:
    """All processes sharing one executable name (a browser is one group)."""

    name: str
    count: int
    memory_bytes: int
    cpu_percent: float  # share of total CPU capacity, 0 to 100


@dataclass(frozen=True)
class ProcessSnapshot:
    total_processes: int
    groups: List[ProcessGroup]


def _require_psutil():
    if psutil is None:
        raise RuntimeError("psutil is not installed")
    return psutil


def cpu_usage() -> CpuUsage:
    ps = _require_psutil()
    return CpuUsage(
        percent=float(ps.cpu_percent(interval=CPU_SAMPLE_SEC)),
        logical_cores=int(ps.cpu_count(logical=True) or 1),
        physical_cores=ps.cpu_count(logical=False),
    )


def memory_usage() -> MemoryUsage:
    mem = _require_psutil().virtual_memory()
    return MemoryUsage(
        used_bytes=int(mem.used), total_bytes=int(mem.total), percent=float(mem.percent)
    )


def disk_usage() -> List[DiskUsage]:
    """Fixed drives only: removable, optical and unreadable volumes are skipped."""
    ps = _require_psutil()
    disks: List[DiskUsage] = []
    for part in ps.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = ps.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append(
            DiskUsage(
                mount=part.mountpoint,
                used_bytes=int(usage.used),
                total_bytes=int(usage.total),
                percent=float(usage.percent),
            )
        )
    return disks


def _run_nvidia_smi() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [exe, f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=GPU_QUERY_TIMEOUT_SEC,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        debug_log(f"nvidia-smi failed: {exc}", "windows")
        return None
    return result.stdout if result.returncode == 0 else None


def _int_or_none(value: str) -> Optional[int]:
    try:
        return int(float(value))
    except ValueError:
        return None  # "[N/A]", "[Not Supported]", ...


def gpu_stats() -> List[GpuStats]:
    """One entry per NVIDIA GPU; empty when none is reliably readable."""
    raw = _run_nvidia_smi()
    if not raw:
        return []
    gpus: List[GpuStats] = []
    for line in raw.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) != 5 or not fields[0]:
            continue
        gpus.append(
            GpuStats(
                name=fields[0],
                utilization_percent=_int_or_none(fields[1]),
                vram_used_mb=_int_or_none(fields[2]),
                vram_total_mb=_int_or_none(fields[3]),
                temperature_c=_int_or_none(fields[4]),
            )
        )
    return gpus


_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_SNAPSHOT_ATTEMPTS = 4
_SYSTEM_PID = 4


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p)]


class _ProcessEntry(ctypes.Structure):
    """Leading fields of ``SYSTEM_PROCESS_INFORMATION`` up to ``WorkingSetSize``."""

    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("NumberOfThreads", ctypes.c_ulong),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", ctypes.c_ulong),
        ("NumberOfThreadsHighWatermark", ctypes.c_ulong),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", _UnicodeString),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", ctypes.c_ulong),
        ("SessionId", ctypes.c_ulong),
        ("UniqueProcessKey", ctypes.c_void_p),
        ("PeakVirtualSize", ctypes.c_size_t),
        ("VirtualSize", ctypes.c_size_t),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
    ]


def _working_set_snapshot() -> Optional[Dict[int, Tuple[str, int]]]:
    """pid -> (image name, working set bytes) for every process, from one system call.

    Returns None off Windows or when the snapshot cannot be read, so callers
    fall back to per-process psutil queries. The working set is the figure
    psutil reports as ``rss`` on Windows.
    """
    if sys.platform != "win32":
        return None
    try:
        query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        query.restype = ctypes.c_ulong
        size = ctypes.c_ulong(1 << 20)
        for _ in range(_SNAPSHOT_ATTEMPTS):
            buffer = ctypes.create_string_buffer(size.value)
            needed = ctypes.c_ulong(0)
            status = query(_SYSTEM_PROCESS_INFORMATION, buffer, size, ctypes.byref(needed))
            if status == _STATUS_INFO_LENGTH_MISMATCH:
                # Processes can start between calls; leave headroom.
                size = ctypes.c_ulong(max(needed.value, size.value) + (1 << 16))
                continue
            if status != 0:
                debug_log(f"process snapshot failed: status {status:#x}", "windows")
                return None
            snapshot: Dict[int, Tuple[str, int]] = {}
            offset = 0
            while True:
                entry = _ProcessEntry.from_buffer(buffer, offset)
                name = entry.ImageName
                image = ctypes.wstring_at(name.Buffer, name.Length // 2) if name.Buffer else ""
                snapshot[int(entry.UniqueProcessId or 0)] = (image, int(entry.WorkingSetSize))
                if not entry.NextEntryOffset:
                    return snapshot
                offset += entry.NextEntryOffset
        debug_log("process snapshot failed: buffer kept growing", "windows")
    except (OSError, AttributeError, ValueError) as exc:
        debug_log(f"process snapshot unavailable: {type(exc).__name__}", "windows")
    return None


def _group_snapshot(working_sets: Dict[int, Tuple[str, int]]) -> tuple[int, Dict[str, list]]:
    """Group a working-set snapshot like ``_group_processes`` groups psutil data."""
    groups: Dict[str, list] = {}
    total = 0
    for pid, (image, working_set) in working_sets.items():
        if pid == 0:
            continue
        total += 1
        name = image or ("System" if pid == _SYSTEM_PID else "")
        if not name:
            continue
        entry = groups.setdefault(name.lower(), [name, 0, 0, 0.0])
        entry[1] += 1
        entry[2] += working_set
    return total, groups


def _group_processes(measure_cpu: bool) -> tuple[int, Dict[str, list]]:
    """Return (total process count, readable groups keyed by lowercase name).

    pid 0 is the "System Idle Process" placeholder and is not a real process.
    Memory figures come from one system snapshot when available. Otherwise
    processes that deny access are counted but excluded from the groups.
    """
    if not measure_cpu:
        working_sets = _working_set_snapshot()
        if working_sets is not None:
            return _group_snapshot(working_sets)
        debug_log("process memory from per-process queries", "windows")
    ps = _require_psutil()
    procs = [p for p in ps.process_iter(["pid", "name"]) if p.info.get("pid") != 0]

    if measure_cpu:
        for proc in procs:
            try:
                proc.cpu_percent(None)  # prime the per-process counter
            except (ps.AccessDenied, ps.NoSuchProcess, OSError):
                pass
        time.sleep(CPU_SAMPLE_SEC)

    cores = int(ps.cpu_count(logical=True) or 1)
    groups: Dict[str, list] = {}
    for proc in procs:
        name = proc.info.get("name")
        if not name:
            continue
        try:
            # Each ranking reads only the figure it sorts by; the per-process
            # memory query is the slow part of a 500+ process listing, which
            # is why the snapshot above is preferred.
            rss = 0 if measure_cpu else int(proc.memory_info().rss)
            cpu = float(proc.cpu_percent(None)) / cores if measure_cpu else 0.0
        except (ps.AccessDenied, ps.NoSuchProcess, OSError):
            continue
        entry = groups.setdefault(name.lower(), [name, 0, 0, 0.0])
        entry[1] += 1
        entry[2] += rss
        entry[3] += cpu
    return len(procs), groups


def _to_groups(groups: Dict[str, list]) -> List[ProcessGroup]:
    return [ProcessGroup(n, c, m, cpu) for n, c, m, cpu in groups.values()]


def top_processes(by: str, limit: int = 5) -> List[ProcessGroup]:
    """Executables ranked by combined working-set memory or CPU share."""
    if by not in _RANKINGS:
        raise ValueError(f"cannot rank processes by {by!r}; use one of {_RANKINGS}")
    _, groups = _group_processes(measure_cpu=(by == "cpu"))
    key = (lambda g: g.memory_bytes) if by == "memory" else (lambda g: g.cpu_percent)
    ranked = sorted(_to_groups(groups), key=key, reverse=True)
    debug_log(f"top processes by {by}: {len(ranked)} groups", "windows")
    return ranked[:limit]


def running_processes(limit: int = 15) -> ProcessSnapshot:
    """Total process count plus the largest executables by memory."""
    total, groups = _group_processes(measure_cpu=False)
    ranked = sorted(_to_groups(groups), key=lambda g: g.memory_bytes, reverse=True)
    return ProcessSnapshot(total_processes=total, groups=ranked[:limit])
