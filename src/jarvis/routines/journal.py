"""The recent-actions journal: tool calls Jarvis executed successfully, kept in memory only.

Saving a routine builds it from these entries, never from text a model writes. ``run_tool_with_retries``
records every successful call on every route; readers take only entries younger than the dialogue
memory window. Entries are never written to disk, the diary, the database or logs. See
``routines.spec.md``, Recent actions.
"""
from __future__ import annotations

from collections import deque
import copy
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Mapping, Optional

from ..debug import debug_log
from .definitions import NEVER_STEPS

MAX_ENTRIES = 20


@dataclass(frozen=True)
class Entry:
    number: int
    tool: str
    args: Mapping[str, Any]
    at: float


class Journal:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: deque[Entry] = deque(maxlen=MAX_ENTRIES)
        self._next = 1

    def record(self, tool: str, args: Optional[Mapping[str, Any]], schema: Optional[Mapping[str, Any]]) -> None:
        """Record one executed call, keeping only the argument keys the tool's schema declares (routing
        hints such as the fast path's ``match`` are not part of the action)."""
        if not tool or tool in NEVER_STEPS:
            return
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        declared = properties if isinstance(properties, Mapping) else {}
        kept = {key: copy.deepcopy(value) for key, value in (args or {}).items() if key in declared}
        with self._lock:
            self._entries.appendleft(Entry(self._next, tool, kept, self._clock()))
            self._next += 1
            count = len(self._entries)
        debug_log(f"journal recorded a call ({count} kept)", "routines")

    def recent(self, max_age: float) -> list[Entry]:
        """Entries younger than ``max_age`` seconds, newest first."""
        now = self._clock()
        with self._lock:
            return [entry for entry in self._entries if now - entry.at < max_age]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_JOURNAL = Journal()


def get_journal() -> Journal:
    return _JOURNAL
