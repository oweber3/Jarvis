"""The phone's view of Jarvis: a mirror of the one conversation, the phone's queries and Jarvis's state.

The transcript is an in-memory log mirrored from the dialogue memory, so voice, desktop chat and phone
turns all appear and a reply outlives the end of the conversation. Long polls wait on ``rev``, which
changes whenever anything the phone shows changes. See ``remote.spec.md``.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..debug import debug_log

BUSY_NOTICE = "Jarvis is busy with another request."
FAILED_NOTICE = "Jarvis couldn't answer that."
UNAVAILABLE_NOTICE = "Jarvis isn't ready yet."
STOPPED_NOTICE = "Stopped."

MAX_ENTRIES = 200
MAX_QUERIES = 20
SYNC_INTERVAL_SEC = 0.5


@dataclass(frozen=True)
class SubmitResult:
    status: str  # "accepted", "busy" or "unavailable"
    query_id: int


class RemoteHub:
    """Thread-safe state behind the phone app. ``backend`` is a ``DaemonBackend`` or a test fake."""

    def __init__(self, backend, *, max_entries: int = MAX_ENTRIES, max_queries: int = MAX_QUERIES,
                 sync_interval_sec: float = SYNC_INTERVAL_SEC, clock: Callable[[], float] = time.time) -> None:
        self._backend = backend
        self._max_queries = max_queries
        self._interval = sync_interval_sec
        self._clock = clock
        self._cond = threading.Condition()
        self._sync_lock = threading.Lock()
        self._rev = 0
        self._entries: deque = deque(maxlen=max_entries)
        self._next_entry_id = 1
        self._queries: "OrderedDict[int, Dict[str, Any]]" = OrderedDict()
        self._records: Dict[int, Dict[str, Any]] = {}
        self._next_query_id = 1
        self._memory = None
        self._cursor = 0.0
        self._signature = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unsubscribe: Optional[Callable[[], None]] = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Sync in the background and on every assistant state change."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._unsubscribe = self._backend.subscribe_state(lambda _state: self._wake.set())
        self._thread = threading.Thread(target=self._run, name="jarvis-remote-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._cond:
            self._cond.notify_all()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception as exc:  # the mirror must never die
                debug_log(f"remote: hub sync failed: {type(exc).__name__}", "remote")
            self._wake.wait(self._interval)
            self._wake.clear()

    # -- mirroring ---------------------------------------------------------------

    def sync(self) -> None:
        """Mirror new conversation turns and notice state changes."""
        with self._sync_lock:
            memory = self._backend.dialogue_memory()
            if memory is not self._memory:
                self._memory, self._cursor = memory, 0.0
            turns = memory.messages_after(self._cursor) if memory is not None else []
            signature = self._status_signature()
            with self._cond:
                changed = False
                for turn in turns:
                    self._cursor = turn.ts
                    if turn.role in ("user", "assistant") and turn.content:
                        self._append_locked(turn.role, turn.content, turn.ts)
                        changed = True
                if signature != self._signature:
                    self._signature = signature
                    changed = True
                if changed:
                    self._bump_locked()

    def _status_signature(self):
        confirmation = self._backend.pending_confirmation()
        mode = self._backend.reply_mode_state() or {}
        return (self._effective_state(), bool(self._backend.is_busy()),
                confirmation["id"] if confirmation else None, mode.get("mode"), tuple(mode.get("enabled") or ()))

    def _append_locked(self, role: str, text: str, ts: Optional[float] = None) -> None:
        self._entries.append({"id": self._next_entry_id, "role": role, "text": text,
                              "ts": ts if ts is not None else self._clock()})
        self._next_entry_id += 1

    def _bump_locked(self) -> None:
        self._rev += 1
        self._cond.notify_all()

    # -- phone queries -----------------------------------------------------------

    def submit(self, text: str) -> SubmitResult:
        """Send a typed request from the phone through the daemon's text path."""
        with self._cond:
            query_id = self._next_query_id
            self._next_query_id += 1
            self._queries[query_id] = {"status": "pending"}
            self._records[query_id] = {"display": None, "started": False, "cancel": False}
            while len(self._queries) > self._max_queries:
                old, _ = self._queries.popitem(last=False)
                self._records.pop(old, None)
            self._bump_locked()
        debug_log(f"remote: phone query {query_id} submitted", "remote")

        def on_start(display: str) -> None:
            with self._cond:
                record = self._records.get(query_id)
                if record is not None:
                    record["display"], record["started"] = display, True

        def on_busy() -> None:
            self._finish(query_id, "busy", notice=BUSY_NOTICE)

        def on_complete(reply) -> None:
            self.sync()  # the reply's turns are in the conversation before the query reads done
            with self._cond:
                record = self._records.get(query_id) or {}
            if reply is None and record.get("cancel"):
                self._finish(query_id, "stopped", notice=STOPPED_NOTICE)
            elif isinstance(reply, str):
                self._finish(query_id, "done", reply=reply)
            elif not record.get("started"):
                self._finish(query_id, "failed", notice=UNAVAILABLE_NOTICE, unavailable=True)
            else:
                self._finish(query_id, "failed", notice=FAILED_NOTICE, request=record.get("display"))

        try:
            self._backend.submit(text, on_start=on_start, on_complete=on_complete, on_busy=on_busy)
        except Exception as exc:
            debug_log(f"remote: phone query {query_id} could not be submitted: {type(exc).__name__}", "remote")
            self._finish(query_id, "failed", notice=UNAVAILABLE_NOTICE, unavailable=True)

        with self._cond:
            query = self._queries.get(query_id, {})
            record = self._records.get(query_id, {})
            if query.get("status") == "busy":
                status = "busy"
            elif record.get("unavailable"):
                status = "unavailable"
            else:
                status = "accepted"
        return SubmitResult(status=status, query_id=query_id)

    def _finish(self, query_id: int, status: str, *, reply: Optional[str] = None, notice: Optional[str] = None,
                request: Optional[str] = None, unavailable: bool = False) -> None:
        with self._cond:
            query = self._queries.get(query_id)
            if query is None or query["status"] != "pending":
                return
            query["status"] = status
            if status == "done":
                query["reply"] = reply
            record = self._records.get(query_id)
            if record is not None:
                record["unavailable"] = unavailable
            if request:
                self._append_locked("user", request)
            if notice:
                self._append_locked("notice", notice)
            self._bump_locked()
        debug_log(f"remote: phone query {query_id} {status}", "remote")

    def cancel(self) -> None:
        """Stop the phone's query in flight (the reply is dropped, as in the desktop chat)."""
        with self._cond:
            for query_id, query in self._queries.items():
                if query["status"] == "pending":
                    self._records[query_id]["cancel"] = True
        self._backend.cancel()

    def resolve_confirmation(self, request_id: str, approve: bool) -> bool:
        resolved = bool(self._backend.resolve_confirmation(request_id, approve))
        if resolved:
            debug_log(f"remote: confirmation {'approved' if approve else 'denied'} from the phone", "remote")
        self._wake.set()
        self.sync()
        return resolved

    # -- reading -------------------------------------------------------------------

    def _phone_query_pending(self) -> bool:
        with self._cond:
            return any(q["status"] == "pending" for q in self._queries.values())

    def _effective_state(self) -> str:
        # Text queries do not move the voice state, so a phone query in flight reads as thinking.
        return "thinking" if self._phone_query_pending() else self._backend.assistant_state()

    def snapshot(self, after: int) -> Dict[str, Any]:
        with self._cond:
            entries = [dict(e) for e in self._entries if e["id"] > after]
            queries = {str(k): dict(v) for k, v in self._queries.items()}
            rev = self._rev
        return {
            "rev": rev,
            "state": self._effective_state(),
            "busy": bool(self._backend.is_busy()),
            "entries": entries,
            "queries": queries,
            "confirmation": self._backend.pending_confirmation(),
            "mode": self._backend.reply_mode_state(),
            "pc": self._backend.pc_stats(),
        }

    def wait(self, rev: int, after: int, timeout_sec: float) -> Dict[str, Any]:
        """Return as soon as ``rev`` is stale or entries newer than ``after`` exist, or after the timeout."""
        deadline = time.monotonic() + max(0.0, timeout_sec)
        with self._cond:
            while (self._rev == rev and not (self._entries and self._entries[-1]["id"] > after)
                   and not self._stop.is_set()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
        return self.snapshot(after)
