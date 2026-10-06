"""Report CPU, RAM, disk, GPU and process information for this computer."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ....debug import debug_log
from ....platform.windows import telemetry
from ....platform.windows._bounded import run_bounded
from ...base import Tool, ToolContext
from ...types import ToolExecutionResult
from ._common import disabled_result, failure, parse_number

_ACTIONS = ["cpu", "ram", "disk", "gpu", "processes", "top_memory", "top_cpu"]
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 25
_GIB = 1024 ** 3
_MIB = 1024 ** 2


def _gib(n: int) -> str:
    return f"{n / _GIB:.1f} GB"


def _mem(n: int) -> str:
    return _gib(n) if n >= _GIB else f"{n / _MIB:.0f} MB"


def _proc_line(g: telemetry.ProcessGroup, by: str) -> str:
    instances = f" ({g.count} processes)" if g.count > 1 else ""
    value = _mem(g.memory_bytes) if by == "memory" else f"{g.cpu_percent:.1f}% CPU"
    return f"{g.name}{instances}: {value}"


def _limit(args: Dict[str, Any], default: int) -> int:
    value = parse_number(args.get("limit"))
    if value is None or value < 1:
        return default
    return min(int(value), _MAX_LIMIT)


class SystemInfoTool(Tool):
    """Read-only system health and resource usage."""

    timeout_sec = 5.0

    @property
    def name(self) -> str:
        return "systemInfo"

    @property
    def description(self) -> str:
        return (
            "Read this computer's resource usage: CPU load, RAM, disk space, GPU "
            "load / VRAM / temperature, the list of running processes, and which "
            "programs use the most memory or CPU (e.g. 'how much RAM is free?', "
            "'what's using the most CPU?', 'how hot is my GPU?'). Read-only."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": _ACTIONS,
                    "description": (
                        "cpu, ram, disk, gpu, processes (count plus largest), "
                        "top_memory (most RAM) or top_cpu (most CPU)."
                    ),
                },
                "limit": {
                    "type": "number",
                    "description": (
                        f"OPTIONAL. How many programs to list (default {_DEFAULT_LIMIT}, "
                        f"max {_MAX_LIMIT})."
                    ),
                },
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        blocked = disabled_result(context)
        if blocked:
            return blocked

        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        debug_log(f"systemInfo action={action}", "windows")
        if action not in _ACTIONS:
            return failure(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")

        try:
            text = run_bounded(lambda: getattr(self, f"_{action}")(args), self.timeout_sec)
        except Exception as exc:
            debug_log(f"systemInfo {action} failed: {exc}", "windows")
            context.user_print("⚠️ Could not read system information.")
            return failure(f"Could not read system information: {exc}")

        context.user_print(f"📊 {text.splitlines()[0]}")
        return ToolExecutionResult(True, text)

    def _cpu(self, args: Dict[str, Any]) -> str:
        cpu = telemetry.cpu_usage()
        cores = f"{cpu.logical_cores} logical cores"
        return f"CPU usage: {cpu.percent:.0f}% ({cores})"

    def _ram(self, args: Dict[str, Any]) -> str:
        mem = telemetry.memory_usage()
        return (
            f"RAM usage: {mem.percent:.0f}% "
            f"({_gib(mem.used_bytes)} used of {_gib(mem.total_bytes)}, "
            f"{_gib(mem.total_bytes - mem.used_bytes)} free)"
        )

    def _disk(self, args: Dict[str, Any]) -> str:
        disks = telemetry.disk_usage()
        if not disks:
            return "Disk usage is not available."
        lines: List[str] = ["Disk usage:"]
        for d in disks:
            lines.append(
                f"  {d.mount} {d.percent:.0f}% used "
                f"({_gib(d.used_bytes)} of {_gib(d.total_bytes)}, "
                f"{_gib(d.total_bytes - d.used_bytes)} free)"
            )
        return "\n".join(lines)

    def _gpu(self, args: Dict[str, Any]) -> str:
        gpus = telemetry.gpu_stats()
        if not gpus:
            return "GPU statistics are not available (no NVIDIA GPU readable through nvidia-smi)."
        lines: List[str] = []
        for g in gpus:
            parts = []
            if g.utilization_percent is not None:
                parts.append(f"{g.utilization_percent}% utilisation")
            if g.vram_used_mb is not None and g.vram_total_mb is not None:
                parts.append(f"VRAM {g.vram_used_mb} of {g.vram_total_mb} MB used")
            if g.temperature_c is not None:
                parts.append(f"{g.temperature_c}°C")
            lines.append(f"GPU {g.name}: " + ", ".join(parts))
        return "\n".join(lines)

    def _processes(self, args: Dict[str, Any]) -> str:
        snap = telemetry.running_processes(limit=_limit(args, 15))
        lines = [f"{snap.total_processes} processes running. Largest by memory:"]
        lines += [f"  {_proc_line(g, 'memory')}" for g in snap.groups]
        return "\n".join(lines)

    def _top_memory(self, args: Dict[str, Any]) -> str:
        top = telemetry.top_processes("memory", _limit(args, _DEFAULT_LIMIT))
        lines = ["Programs using the most RAM:"]
        lines += [f"  {_proc_line(g, 'memory')}" for g in top]
        return "\n".join(lines)

    def _top_cpu(self, args: Dict[str, Any]) -> str:
        top = telemetry.top_processes("cpu", _limit(args, _DEFAULT_LIMIT))
        lines = ["Programs using the most CPU:"]
        lines += [f"  {_proc_line(g, 'cpu')}" for g in top]
        return "\n".join(lines)
