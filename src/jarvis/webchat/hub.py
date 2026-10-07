"""The web chat's view of Jarvis: which chat is open, what lands in it, and what the page is told.

Jarvis has one dialogue memory, so one chat is open at a time: the one whose turns the memory holds.
The hub mirrors every turn the memory gains into that chat (typed replies, voice and the outcome of a
confirmed action all arrive the same way), swaps the memory when another chat is opened, and keeps the
page's long polls waiting on ``rev``. See ``webchat.spec.md``.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from ..memory.chat_store import ANY_PROJECT, Chat, ChatStore

DEFAULT_CHAT_TITLE = "Voice and quick questions"
RESTORE_TURNS = 20
SYNC_INTERVAL_SEC = 0.5
MAX_NOTICES = 20

NOTICE_TEXT = {
    "busy": "Jarvis is busy with another request.",
    "failed": "Jarvis couldn't answer that.",
    "unavailable": "Jarvis isn't ready yet.",
    "stopped": "Stopped.",
}


@dataclass(frozen=True)
class SubmitResult:
    status: str  # "accepted", "busy" or "unavailable"
    query_id: int


@dataclass(frozen=True)
class OpResult:
    status: str  # "ok", "busy", "unknown" or "unavailable"
    chat: Optional[Chat] = None


@dataclass(frozen=True)
class ModelResult:
    ok: bool
    reason: Optional[str] = None


def chat_dict(chat: Chat) -> Dict[str, Any]:
    return {"id": chat.id, "project_id": chat.project_id, "title": chat.title, "created_at": chat.created_at,
            "updated_at": chat.updated_at, "last_mode": chat.last_mode, "last_model": chat.last_model}


class ChatHub:
    """Thread-safe state behind the web chat. ``backend`` is a ``DaemonBackend`` or a test fake."""

    def __init__(self, backend, store: ChatStore, *, sync_interval_sec: float = SYNC_INTERVAL_SEC,
                 restore_turns: int = RESTORE_TURNS, clock: Callable[[], float] = time.time) -> None:
        self._backend = backend
        self._store = store
        self._interval = sync_interval_sec
        self._restore_turns = restore_turns
        self._clock = clock
        self._cond = threading.Condition()
        self._sync_lock = threading.RLock()
        self._rev = 0
        self._library_rev = 0
        self._notices: deque = deque(maxlen=MAX_NOTICES)
        self._next_notice_id = 1
        self._next_query_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._submitting: set = set()
        self._outcomes: Dict[int, str] = {}
        self._memory = None
        self._cursor = 0.0
        self._signature = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unsubscribe: Optional[Callable[[], None]] = None

    @property
    def store(self) -> ChatStore:
        return self._store

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Mirror in the background and on every assistant state change."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._unsubscribe = self._backend.subscribe_state(lambda _state: self._wake.set())
        self._thread = threading.Thread(target=self._run, name="jarvis-webchat-hub", daemon=True)
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
                debug_log(f"webchat: hub sync failed: {type(exc).__name__}", "webchat")
            self._wake.wait(self._interval)
            self._wake.clear()

    # -- mirroring ---------------------------------------------------------------

    def sync(self) -> None:
        """Mirror new conversation turns into the open chat and notice state changes."""
        with self._sync_lock:
            memory = self._backend.dialogue_memory()
            if memory is not self._memory:
                self._attach(memory)
            changed = False
            chat_id = self._ensure_open_chat()
            if memory is not None and chat_id is not None:
                for turn in memory.messages_after(self._cursor):
                    self._cursor = turn.ts
                    if turn.role in ("user", "assistant") and turn.content:
                        self._store_turn(chat_id, turn)
                        changed = True
            signature = self._signature_now()
            with self._cond:
                if signature != self._signature:
                    self._signature = signature
                    changed = True
                if changed:
                    self._library_rev += 1
                    self._bump_locked()

    def _attach(self, memory) -> None:
        """Start following a (new) dialogue memory. An empty one gets the open chat's recent turns back."""
        self._memory, self._cursor = memory, 0.0
        if memory is None:
            return
        chat_id = self._ensure_open_chat()
        if chat_id is not None and not memory.all_messages():
            turns = self._restore_payload(chat_id)
            if turns and self._backend.switch_conversation(turns):
                self._cursor = memory.last_timestamp()
                debug_log("webchat: restored the open chat into the conversation", "webchat")

    def _ensure_open_chat(self) -> Optional[str]:
        chat_id = self._store.get_active_chat_id()
        if chat_id is not None and self._store.get_chat(chat_id) is not None:
            return chat_id
        chats = self._store.list_chats(ANY_PROJECT)
        chat = chats[0] if chats else self._store.create_chat(title=DEFAULT_CHAT_TITLE)
        self._store.set_active_chat_id(chat.id)
        return chat.id

    def _store_turn(self, chat_id: str, turn) -> None:
        last = self._store.last_messages(chat_id, 1)
        if self._typed_in_flight():
            source = "typed"
        elif turn.role == "assistant" and last and last[0].role == "assistant":
            source = "confirmed"  # a reply with no request before it: the outcome of a confirmed action
        elif turn.role == "assistant" and last:
            source = last[0].source
        else:
            source = "voice"
        self._store.append_message(chat_id, turn.role, turn.content, ts=turn.ts, source=source,
                                   private=bool(turn.private))
        if turn.role == "assistant":
            self._remember_model()

    def _restore_payload(self, chat_id: str) -> List[dict]:
        return [{"role": m.role, "content": m.content, **({"diary": False} if m.private else {})}
                for m in self._store.last_messages(chat_id, self._restore_turns)]

    def _typed_in_flight(self) -> bool:
        with self._cond:
            return bool(self._pending)

    def _signature_now(self):
        mode = self._backend.reply_mode_state() or {}
        model = self._backend.local_model_state() or {}
        cloud = self._backend.cloud_model_state() or {}
        return (self._effective_state(), bool(self._backend.is_busy()), mode.get("mode"),
                tuple(mode.get("enabled") or ()), model.get("current"), self._backend.is_ready(),
                tuple(sorted(cloud.items())))

    def _bump_locked(self) -> None:
        self._rev += 1
        self._cond.notify_all()

    # -- chats ---------------------------------------------------------------------

    def open_chat(self, chat_id: str) -> OpResult:
        """Make ``chat_id`` the open chat: the conversation becomes its history."""
        with self._sync_lock:
            self.sync()
            chat = self._store.get_chat(chat_id)
            if chat is None:
                return OpResult("unknown")
            if not self._backend.is_ready():
                return OpResult("unavailable")
            if self._typed_in_flight():
                return OpResult("busy")
            if not self._backend.switch_conversation(self._restore_payload(chat_id)):
                return OpResult("busy")
            self._opened(chat_id)
            return OpResult("ok", self._store.get_chat(chat_id))

    def new_chat(self, project_id: Optional[str] = None) -> OpResult:
        """Create a chat (optionally inside a project) and open it with an empty conversation."""
        with self._sync_lock:
            self.sync()
            if project_id is not None and all(p.id != project_id for p in self._store.list_projects()):
                return OpResult("unknown")
            if not self._backend.is_ready():
                return OpResult("unavailable")
            if self._typed_in_flight() or not self._backend.switch_conversation([]):
                return OpResult("busy")
            chat = self._store.create_chat(project_id=project_id)
            self._opened(chat.id)
            return OpResult("ok", chat)

    def delete_chat(self, chat_id: str) -> str:
        with self._sync_lock:
            self.sync()
            if self._store.get_chat(chat_id) is None:
                return "unknown"
            if self._store.get_active_chat_id() == chat_id:
                # The next chat becomes the open one with its conversation, so what the page shows is
                # what Jarvis remembers; with no other chat the conversation starts empty.
                remaining = [c for c in self._store.list_chats(ANY_PROJECT) if c.id != chat_id]
                target = remaining[0].id if remaining else None
                if self._typed_in_flight() or not self._backend.switch_conversation(
                        self._restore_payload(target) if target else []):
                    return "busy"
                self._store.delete_chat(chat_id)
                self._opened(target)
            else:
                self._store.delete_chat(chat_id)
                self._bump_library()
            return "ok"

    def delete_all(self) -> str:
        """Remove every project, chat and message, and empty the conversation."""
        with self._sync_lock:
            self.sync()
            if self._typed_in_flight() or not self._backend.switch_conversation([]):
                return "busy"
            self._store.delete_all()
            self._opened(None)
            return "ok"

    def _opened(self, chat_id: Optional[str]) -> None:
        self._store.set_active_chat_id(chat_id)
        self._cursor = self._memory.last_timestamp() if self._memory is not None else 0.0
        with self._cond:
            self._notices.clear()
            self._library_rev += 1
            self._bump_locked()
        debug_log("webchat: chat opened", "webchat")

    def _bump_library(self) -> None:
        with self._cond:
            self._library_rev += 1
            self._bump_locked()

    def changed_library(self) -> None:
        """Tell the page the project or chat lists changed (after a rename, move or project edit)."""
        self._bump_library()

    # -- requests ----------------------------------------------------------------

    def submit(self, text: str) -> SubmitResult:
        """Send a typed request through the daemon's text path, into the open chat."""
        with self._sync_lock:
            self.sync()
            self._ensure_open_chat()
            # Registered before the lock is released, so no chat can be opened between here and the daemon.
            with self._cond:
                query_id = self._next_query_id
                self._next_query_id += 1
                self._pending[query_id] = {"display": None, "started": False, "cancel": False, "status": "pending"}
                self._bump_locked()
        debug_log(f"webchat: query {query_id} submitted", "webchat")

        def on_start(display: str) -> None:
            with self._cond:
                record = self._pending.get(query_id)
                if record is not None:
                    record["display"], record["started"] = display, True

        def on_busy() -> None:
            self._finish(query_id, "busy")

        def on_complete(reply) -> None:
            self.sync()  # the reply's turns are in the chat before the query reads done
            with self._cond:
                record = dict(self._pending.get(query_id) or {})
            if reply is None and record.get("cancel"):
                self._finish(query_id, "stopped")
            elif isinstance(reply, str):
                self._finish(query_id, "done")
            elif not record.get("started"):
                self._finish(query_id, "unavailable")
            else:
                self._finish(query_id, "failed", request=record.get("display"))

        with self._cond:
            self._submitting.add(query_id)
        try:
            self._backend.submit(text, on_start=on_start, on_complete=on_complete, on_busy=on_busy)
        except Exception as exc:
            debug_log(f"webchat: query {query_id} could not be submitted: {type(exc).__name__}", "webchat")
            self._finish(query_id, "unavailable")
        with self._cond:
            self._submitting.discard(query_id)
            outcome = self._outcomes.pop(query_id, "accepted")
        return SubmitResult(status=outcome if outcome in ("busy", "unavailable") else "accepted", query_id=query_id)

    def _finish(self, query_id: int, status: str, *, request: Optional[str] = None) -> None:
        with self._cond:
            record = self._pending.pop(query_id, None)
            if record is None:
                return
            if query_id in self._submitting:
                self._outcomes[query_id] = status
            if status in NOTICE_TEXT:
                self._notices.append({"id": self._next_notice_id, "kind": status, "text": NOTICE_TEXT[status],
                                      "request": request, "ts": self._clock()})
                self._next_notice_id += 1
            self._bump_locked()
        if status == "done":
            self._remember_model()
        debug_log(f"webchat: query {query_id} {status}", "webchat")

    def _remember_model(self) -> None:
        """Record which reply mode and local model answered, on the open chat."""
        chat_id = self._store.get_active_chat_id()
        if chat_id is None:
            return
        mode = (self._backend.reply_mode_state() or {}).get("mode") or ""
        cloud = self._backend.cloud_model_state()
        model = (cloud["model"] if cloud else (self._backend.local_model_state() or {}).get("current")) or ""
        self._store.set_last_model(chat_id, mode, model)

    def cancel(self) -> None:
        """Stop the request in flight (the reply is dropped, as in the desktop chat)."""
        with self._cond:
            for record in self._pending.values():
                record["cancel"] = True
        self._backend.cancel()

    # -- models --------------------------------------------------------------------

    def switch_mode(self, mode: str) -> ModelResult:
        ok, reason = self._backend.switch_reply_mode(mode)
        self._wake.set()
        self.sync()
        return ModelResult(ok, reason)

    def set_cloud_model(self, model: str, effort: Optional[str]) -> ModelResult:
        ok, reason = self._backend.set_cloud_model(model, effort)
        self._wake.set()
        self.sync()
        return ModelResult(ok, reason)

    def set_local_model(self, model: str) -> ModelResult:
        ok, reason = self._backend.set_local_model(model)
        self._wake.set()
        self.sync()
        return ModelResult(ok, reason)

    # -- reading ---------------------------------------------------------------------

    def _effective_state(self) -> str:
        # Text queries do not move the voice state, so a typed request in flight reads as thinking.
        return "thinking" if self._typed_in_flight() else self._backend.assistant_state()

    def models(self) -> Dict[str, Any]:
        """What the model selector offers: the allowed reply modes and the local models."""
        state = self._backend.local_model_state() or {}
        cloud = self._backend.cloud_model_state()
        return {"mode": self._backend.reply_mode_state(), "current": state.get("current"),
                "switchable": bool(state.get("switchable")), "models": self._backend.local_models(),
                "cloud": {**cloud, "models": self._backend.cloud_models()} if cloud else None}

    def library(self) -> Dict[str, Any]:
        if self._backend.is_ready():
            self._ensure_open_chat()
        return {
            "projects": [{"id": p.id, "name": p.name, "position": p.position} for p in self._store.list_projects()],
            "chats": [chat_dict(c) for c in self._store.list_chats(ANY_PROJECT)],
            "active_chat_id": self._store.get_active_chat_id(),
        }

    def snapshot(self, after: int) -> Dict[str, Any]:
        active = self._store.get_active_chat_id()
        chat = self._store.get_chat(active) if active else None
        messages = self._store.messages(active, after) if chat is not None else []
        with self._cond:
            notices = [dict(n) for n in self._notices]
            rev, library_rev = self._rev, self._library_rev
        return {
            "rev": rev,
            "library_rev": library_rev,
            "ready": bool(self._backend.is_ready()),
            "state": self._effective_state(),
            "busy": bool(self._backend.is_busy()),
            "busy_query": self._typed_in_flight(),
            "active_chat_id": chat.id if chat else None,
            "chat": chat_dict(chat) if chat else None,
            "messages": [{"id": m.id, "role": m.role, "text": m.content, "ts": m.ts, "source": m.source}
                         for m in messages],
            "notices": notices,
            "mode": self._backend.reply_mode_state(),
            "model": self._backend.local_model_state(),
            "cloud": self._backend.cloud_model_state(),
        }

    def wait(self, rev: int, after: int, timeout_sec: float) -> Dict[str, Any]:
        """Return as soon as ``rev`` is stale or the open chat has messages newer than ``after``, or after the timeout."""
        deadline = time.monotonic() + max(0.0, timeout_sec)
        active = self._store.get_active_chat_id()
        newer = bool(active) and bool(self._store.messages(active, after))
        with self._cond:
            while self._rev == rev and not newer and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
        return self.snapshot(after)
