"""Tool steps: one short line for every call that went through central safety, kept in memory.

``run_tool_with_retries`` records a step when a call ran, failed, was refused or is waiting for a yes, on
every route (fast path, local loop, planner, bridges, approved confirmations, routine steps). The web
chat reads them and stores them with the reply they belong to. Nothing here is written to disk, logs or
the diary, and a step holds only a tool name, a status and words from the tool's own ``step_note`` (never
arguments, results or error text). See ``webchat/webchat.spec.md``, Tool steps.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import re
import threading
import time
from typing import Callable, Optional

from ..debug import debug_log

MAX_STEPS = 200
LABEL_MAX_CHARS = 80
SUMMARY_MAX_CHARS = 160

STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_DECLINED = "declined"
STATUS_WAITING = "waiting"
STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_DECLINED, STATUS_WAITING)

# Text that names a place or a person rather than an action: drive and network paths, absolute paths,
# addresses, e-mail addresses. A tool's words that match are dropped, so a careless note cannot leak one.
_SENSITIVE = re.compile(
    r"[A-Za-z]:[\\/]|\\|://|\bwww\.|(?:^|\s)[~.]{0,2}/[^\s/]+/|\S+@\S+\.\S+",
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class ToolStep:
    seq: int
    ts: float
    tool: str
    status: str
    label: str
    summary: Optional[str]


def humanise(tool_name: str) -> str:
    """A readable name from a tool's identifier: ``systemVolume`` is "System volume". An MCP tool
    (``server__fetch_page``) is named by its own part."""
    base = (tool_name or "").rsplit("__", 1)[-1]
    words = _CAMEL_BOUNDARY.sub(" ", base).replace("_", " ").replace("-", " ").split()
    if not words:
        return "Tool"
    text = " ".join(words).lower()
    return text[0].upper() + text[1:]


def clean_text(text: Optional[str], limit: int) -> Optional[str]:
    """One short line, or ``None`` when there is nothing to show or it names a path, address or person."""
    if not isinstance(text, str):
        return None
    line = " ".join(text.split())
    if not line or _SENSITIVE.search(line):
        return None
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


class StepLog:
    """A bounded, thread-safe record of tool steps, numbered in the order they were recorded."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._steps: deque[ToolStep] = deque(maxlen=MAX_STEPS)
        self._next = 1

    def record(self, tool: str, status: str, label: Optional[str] = None,
               summary: Optional[str] = None) -> ToolStep:
        """Add a step. A label that is empty or names a path or address becomes the tool's own name, and a
        summary like that is dropped."""
        step_label = clean_text(label, LABEL_MAX_CHARS) or humanise(tool)
        step_summary = clean_text(summary, SUMMARY_MAX_CHARS)
        with self._lock:
            step = ToolStep(self._next, self._clock(), tool, status, step_label, step_summary)
            self._steps.append(step)
            self._next += 1
        debug_log(f"tool step recorded ({status})", "tools")
        return step

    def since(self, seq: int, until_ts: Optional[float] = None) -> list[ToolStep]:
        """Steps numbered after ``seq``, oldest first, and (when given) recorded no later than ``until_ts``."""
        with self._lock:
            return [s for s in self._steps if s.seq > seq and (until_ts is None or s.ts <= until_ts)]

    def latest(self) -> int:
        """The number of the newest step, 0 when there is none."""
        with self._lock:
            return self._steps[-1].seq if self._steps else 0


_LOG = StepLog()


def get_step_log() -> StepLog:
    return _LOG
