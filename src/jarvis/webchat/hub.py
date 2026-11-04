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
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..debug import debug_log
from ..memory.chat_store import ANY_PROJECT, Chat, ChatStore, Message
from ..tools.step_log import StepLog, get_step_log, humanise
from . import suggestions
from .mic import MicSessions
from ..reply.attachments import (KIND_IMAGE, MAX_ATTACHMENTS, MAX_FILE_CHARS, MAX_IMAGES, RequestAttachment,
                                 attachment_summary)
from . import uploads
from .uploads import UploadRefused, UploadStore, classify

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
    # "accepted", "busy" or "unavailable"; a rewind can also be "unknown", "not_open", "not_user" or "out_of_step"
    status: str
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
            "updated_at": chat.updated_at, "last_mode": chat.last_mode, "last_model": chat.last_model,
            "pinned": chat.pinned}


def message_dict(message: Message) -> Dict[str, Any]:
    """A stored message as the page gets it: never the private flag, steps as stored, attachments as kind and size only."""
    return {"id": message.id, "role": message.role, "text": message.content, "ts": message.ts,
            "source": message.source, "steps": [dict(step) for step in message.steps],
            "attachments": [dict(a) for a in message.attachments]}


class ChatHub:
    """Thread-safe state behind the web chat. ``backend`` is a ``DaemonBackend`` or a test fake."""

    def __init__(self, backend, store: ChatStore, *, sync_interval_sec: float = SYNC_INTERVAL_SEC,
                 restore_turns: int = RESTORE_TURNS, clock: Callable[[], float] = time.time,
                 steps: Optional[StepLog] = None) -> None:
        self._backend = backend
        self._steps = steps if steps is not None else get_step_log()
        # Steps up to here belong to nothing the open chat shows: they were taken before it was open.
        self._step_cursor = self._steps.latest()
        self._store = store
        self._interval = sync_interval_sec
        self._restore_turns = restore_turns
        self._clock = clock
        self._cond = threading.Condition()
        self._sync_lock = threading.RLock()
        self._rev = 0
        self._library_rev = 0
        # Bumped when stored messages of the open chat are taken back (a Rewind), so every page reloads it.
        self._history_rev = 0
        self._notices: deque = deque(maxlen=MAX_NOTICES)
        self._next_notice_id = 1
        self._next_query_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._uploads = UploadStore()
        self._submitting: set = set()
        self._outcomes: Dict[int, str] = {}
        self._memory = None
        self._cursor = 0.0
        self._signature = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._users = 0
        self._watches: List[Callable[[], Any]] = []
        self.mic = MicSessions(backend)

    @property
    def store(self) -> ChatStore:
        return self._store

    @property
    def uploads(self) -> UploadStore:
        return self._uploads

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Mirror in the background and on every assistant state change.

        The hub is shared by the web chat and phone access: every ``start`` is paired with one ``stop``,
        and the mirror runs until the last user has stopped."""
        with self._cond:
            self._users += 1
        if self._thread is not None:
            return
        self._stop.clear()
        self._unsubscribe = self._backend.subscribe_state(lambda _state: self._wake.set())
        self._thread = threading.Thread(target=self._run, name="jarvis-webchat-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._users = max(0, self._users - 1)
            if self._users > 0:
                return
        self._stop.set()
        self._wake.set()
        self.mic.shutdown()
        with self._sync_lock:
            self._let_go_of_memory()
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
        """Start following a (new) dialogue memory. An empty one gets the open chat's recent turns back.

        The open chat's latest turns stay in the reply context however old they are, so the model
        keeps the thread the page shows while the owner reads."""
        self._let_go_of_memory()
        self._memory, self._cursor = memory, 0.0
        if memory is None:
            return
        memory.keep_last_turns(self._restore_turns)
        debug_log(f"webchat: keeping the open chat's last {self._restore_turns} turns in context", "webchat")
        chat_id = self._ensure_open_chat()
        if chat_id is not None and not memory.all_messages():
            turns = self._restore_payload(chat_id)
            cursor = self._backend.switch_conversation(turns) if turns else None
            if cursor is not None:
                self._cursor = cursor
                debug_log("webchat: restored the open chat into the conversation", "webchat")

    def _let_go_of_memory(self) -> None:
        """Hand the memory back to the dialogue window alone (the web chat stops or the memory changes)."""
        if self._memory is not None:
            self._memory.keep_last_turns(0)

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
            source = self._inflight_source()
        elif turn.role == "assistant" and last and last[0].role == "assistant":
            source = "confirmed"  # a reply with no request before it: the outcome of a confirmed action
        elif turn.role == "assistant" and last:
            source = last[0].source
        else:
            source = "voice"
        steps = self._take_steps(turn) if turn.role == "assistant" else []
        self._store.append_message(chat_id, turn.role, turn.content, ts=turn.ts, source=source,
                                   private=bool(turn.private), private_sources=turn.private_sources,
                                   steps=steps,
                                   attachments=self._claim_attachments() if turn.role == "user" else ())
        if turn.role == "assistant":
            self._remember_model()

    def _take_steps(self, turn) -> List[Dict[str, Any]]:
        """The tool steps taken since the last reply and no later than this one, as stored with it. A
        reply that quotes the activity log keeps only the tool and its status, never what it did."""
        found = self._steps.since(self._step_cursor, until_ts=turn.ts)
        if not found:
            return []
        self._step_cursor = found[-1].seq
        debug_log(f"webchat: {len(found)} tool steps stored with a reply", "webchat")
        return [{"tool": s.tool, "status": s.status,
                 "label": humanise(s.tool) if turn.private else s.label,
                 "summary": None if turn.private else s.summary} for s in found]

    def _drop_steps(self) -> None:
        """Forget the steps taken so far: a request that gave no reply must not lend them to the next one."""
        self._step_cursor = self._steps.latest()

    def _restore_payload(self, chat_id: str) -> List[dict]:
        return [self._turn_payload(m) for m in self._store.last_messages(chat_id, self._restore_turns)]

    @staticmethod
    def _turn_payload(m: Message) -> dict:
        """A stored message as the daemon takes a conversation's turn: a private one keeps its sources."""
        return {"role": m.role, "content": m.content,
                **({"diary": False, "private_sources": list(m.private_sources)} if m.private else {})}

    def _typed_in_flight(self) -> bool:
        with self._cond:
            return bool(self._pending)

    def _inflight_source(self) -> str:
        """``typed``, or ``voice`` for a spoken phone request, of the request in flight."""
        with self._cond:
            for record in self._pending.values():
                return record.get("source", "typed")
        return "typed"

    def _claim_attachments(self) -> List[Dict[str, Any]]:
        """The kind and size of what the typed request in flight carried, once, for its user turn."""
        with self._cond:
            for record in self._pending.values():
                if record.get("attachments") and not record.get("claimed"):
                    record["claimed"] = True
                    return list(record["attachments"])
        return []

    def _signature_now(self):
        mode = self._backend.reply_mode_state() or {}
        model = self._backend.local_model_state() or {}
        cloud = self._backend.cloud_model_state() or {}
        # A step taken for a typed request in flight changes what the page shows while Jarvis works.
        working = self._steps.latest() if self._typed_in_flight() else None
        return (self._effective_state(), bool(self._backend.is_busy()), mode.get("mode"),
                tuple(mode.get("enabled") or ()), model.get("current"), self._backend.is_ready(),
                tuple(sorted(cloud.items())), self._watched(), working)

    def _live_steps(self) -> List[Dict[str, Any]]:
        """The steps taken so far for the typed request in flight (the ones its reply will be stored with), with
        their tool, status and label only: what was found arrives with the reply, as stored."""
        if not self._typed_in_flight():
            return []
        return [{"tool": s.tool, "status": s.status, "label": s.label} for s in self._steps.since(self._step_cursor)]

    def _watched(self) -> tuple:
        """The values of the extra change signals; one that raises reads as unchanged."""
        values = []
        for watch in list(self._watches):
            try:
                values.append(watch())
            except Exception as exc:
                debug_log(f"webchat: a change signal failed: {type(exc).__name__}", "webchat")
                values.append(None)
        return tuple(values)

    def watch(self, signal: Callable[[], Any]) -> None:
        """Wake the long polls whenever ``signal()`` returns a different (comparable) value. Phone access
        uses it for a pending confirmation and for reply audio."""
        self._watches.append(signal)

    def unwatch(self, signal: Callable[[], Any]) -> None:
        """Stop following a signal given to ``watch`` (a server using the shared hub stops)."""
        if signal in self._watches:
            self._watches.remove(signal)

    def poke(self) -> None:
        """Tell every waiting poll that something outside the chat changed."""
        with self._cond:
            self._bump_locked()

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
            cursor = self._backend.switch_conversation(self._restore_payload(chat_id))
            if cursor is None:
                return OpResult("busy")
            self._opened(chat_id, cursor)
            return OpResult("ok", self._store.get_chat(chat_id))

    def new_chat(self, project_id: Optional[str] = None) -> OpResult:
        """Create a chat (optionally inside a project) and open it with an empty conversation."""
        with self._sync_lock:
            self.sync()
            if project_id is not None and all(p.id != project_id for p in self._store.list_projects()):
                return OpResult("unknown")
            if not self._backend.is_ready():
                return OpResult("unavailable")
            cursor = None if self._typed_in_flight() else self._backend.switch_conversation([])
            if cursor is None:
                return OpResult("busy")
            chat = self._store.create_chat(project_id=project_id)
            self._opened(chat.id, cursor)
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
                cursor = None if self._typed_in_flight() else self._backend.switch_conversation(
                    self._restore_payload(target) if target else [])
                if cursor is None:
                    return "busy"
                self._store.delete_chat(chat_id)
                self._opened(target, cursor)
            else:
                self._store.delete_chat(chat_id)
                self._bump_library()
            return "ok"

    def delete_all(self) -> str:
        """Remove every project, chat and message, and empty the conversation."""
        with self._sync_lock:
            self.sync()
            cursor = None if self._typed_in_flight() else self._backend.switch_conversation([])
            if cursor is None:
                return "busy"
            self._store.delete_all()
            self._uploads.clear()
            self._opened(None, cursor)
            return "ok"

    def _opened(self, chat_id: Optional[str], cursor: float) -> None:
        """Record ``chat_id`` as open and mirror from ``cursor``, the point the swap returned, so a turn
        that lands right after the swap still reaches the new chat."""
        self._store.set_active_chat_id(chat_id)
        self._cursor = cursor
        self._drop_steps()
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

    def submit(self, text: str, attachment_ids: Sequence[str] = (), *, origin: str = "chat",
               source: str = "typed", on_finish: Optional[Callable[[int, str, Optional[str]], None]] = None
               ) -> SubmitResult:
        """Send a request through the daemon's text path, into the open chat. ``attachment_ids`` are
        uploads waiting in the store: they go with this request only. An id that is unknown, or an image the
        active route cannot see, refuses the whole request and keeps the uploads for another try.

        ``origin`` is the daemon's request origin (``chat``, or ``phone`` for a paired phone) and ``source`` how
        the owner made the request (``typed``, or ``voice`` for a spoken phone request), which is how its turns
        are stored. ``on_finish(query_id, status, reply)`` is called once, after the reply is in the chat, with
        ``done`` (and the reply text), ``busy``, ``stopped``, ``unavailable`` or ``failed``."""
        with self._sync_lock:
            self.sync()
            self._ensure_open_chat()
            if attachment_ids:
                self._check_images(self._uploads.peek(attachment_ids))
            # Registered before the lock is released, so no chat can be opened between here and the daemon.
            query_id = self._register(source, on_finish)
            attachments: List[RequestAttachment] = []
            if attachment_ids:
                try:
                    attachments = self._uploads.take(attachment_ids)
                except UploadRefused:
                    with self._cond:
                        self._pending.pop(query_id, None)
                        self._bump_locked()
                    raise
                with self._cond:
                    self._pending[query_id]["attachments"] = attachment_summary(attachments)
        debug_log(f"webchat: query {query_id} submitted" + (f" with {len(attachments)} attachment(s)"
                                                            if attachments else ""), "webchat")
        extra = {"attachments": tuple(attachments)} if attachments else {}
        if origin != "chat":
            extra["origin"] = origin
        return self._send(query_id, lambda **callbacks: self._backend.submit(text, **callbacks, **extra))

    def rewind(self, chat_id: str, message_id: int, text: Optional[str] = None, *, newest_id: int,
               origin: str = "chat", source: str = "typed",
               on_finish: Optional[Callable[[int, str, Optional[str]], None]] = None) -> SubmitResult:
        """Ask a stored message of the open chat again, as it was or as ``text`` (edit and regenerate). It and every
        message after it leave the chat and the conversation, under the daemon's query lock, before the request
        runs; the new turns then land as a typed request's do. ``newest_id`` is the newest stored message the page
        asking holds, so it has shown the owner everything that would go. Besides a request's outcomes the status
        can be ``unknown`` (no such chat or message), ``not_open``, ``not_user`` (a reply, not the owner's
        message), ``changed`` (the chat's newest message is not ``newest_id``: it gained or lost messages the page
        has not shown), ``out_of_step`` (the conversation does not hold the chat's newest turns) or
        ``not_taken_back`` (the store could not remove them, so the conversation was put back); none of the last
        three changes anything."""
        with self._sync_lock:
            self.sync()
            if self._store.get_chat(chat_id) is None:
                return SubmitResult("unknown", 0)
            if self._store.get_active_chat_id() != chat_id:
                return SubmitResult("not_open", 0)
            stored = self._store.messages(chat_id)
            index = next((i for i, m in enumerate(stored) if m.id == message_id), None)
            if index is None:
                return SubmitResult("unknown", 0)
            if stored[index].role != "user":
                return SubmitResult("not_user", 0)
            if stored[-1].id != newest_id:
                return SubmitResult("changed", 0)
            if not self._backend.is_ready():
                return SubmitResult("unavailable", 0)
            if self._typed_in_flight():
                return SubmitResult("busy", 0)
            words = stored[index].content if text is None else text
            history = [self._turn_payload(m) for m in stored]
            query_id = self._register(source, on_finish)

            def on_rewind(cursor: Optional[float]) -> bool:
                if cursor is None:
                    self._finish(query_id, "out_of_step")
                    return False
                # Still inside this call and the sync lock, so no mirror pass can run between the swap and here.
                try:
                    self._store.delete_messages_from(chat_id, message_id)
                except Exception as exc:  # the daemon puts the conversation back: nothing changed anywhere
                    debug_log(f"webchat: query {query_id} could not take messages back: {type(exc).__name__}",
                              "webchat")
                    self._finish(query_id, "not_taken_back")
                    return False
                self._cursor = cursor
                self._drop_steps()
                with self._cond:
                    self._notices.clear()
                    self._history_rev += 1
                    self._bump_locked()
                return True

            debug_log(f"webchat: query {query_id} asks a stored message again", "webchat")
            return self._send(query_id, lambda **callbacks: self._backend.rewind(
                history, index, words, on_rewind=on_rewind, origin=origin, **callbacks))

    def _register(self, source: str, on_finish) -> int:
        """Track a request from the moment it is accepted: typed turns are claimed for it from here on."""
        with self._cond:
            query_id = self._next_query_id
            self._next_query_id += 1
            self._pending[query_id] = {"display": None, "started": False, "cancel": False, "status": "pending",
                                       "source": source, "on_finish": on_finish}
            self._bump_locked()
        return query_id

    def _send(self, query_id: int, start: Callable[..., None]) -> SubmitResult:
        """Run ``start(on_start=, on_complete=, on_busy=)`` for a registered request and say how it was taken."""

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
                self._finish(query_id, "stopped", request=record.get("display"))
            elif isinstance(reply, str):
                self._finish(query_id, "done", reply=reply)
            elif not record.get("started"):
                self._finish(query_id, "unavailable")
            else:
                self._finish(query_id, "failed", request=record.get("display"))

        with self._cond:
            self._submitting.add(query_id)
        try:
            start(on_start=on_start, on_complete=on_complete, on_busy=on_busy)
        except Exception as exc:
            debug_log(f"webchat: query {query_id} could not be submitted: {type(exc).__name__}", "webchat")
            self._finish(query_id, "unavailable")
        with self._cond:
            self._submitting.discard(query_id)
            outcome = self._outcomes.pop(query_id, "accepted")
        refused = outcome in ("busy", "unavailable", "out_of_step", "not_taken_back")
        return SubmitResult(status=outcome if refused else "accepted", query_id=query_id)

    def _check_images(self, held: Sequence[RequestAttachment]) -> None:
        """Refuse an image the active route cannot see. A local model without vision still gets a screenshot's
        text; a cloud mode that takes no images gets neither."""
        images = (self._backend.attachment_support() or {}).get("images") or {}
        if images.get("ok", True):
            return
        reason = images.get("reason") or "no_vision"
        if any(a.kind == KIND_IMAGE for a in held) or (reason != "no_vision" and any(a.image for a in held)):
            debug_log(f"webchat: attachments refused ({reason})", "webchat")
            raise UploadRefused(reason)

    def _finish(self, query_id: int, status: str, *, request: Optional[str] = None,
                reply: Optional[str] = None) -> None:
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
        elif status != "busy":
            self._drop_steps()
        debug_log(f"webchat: query {query_id} {status}", "webchat")
        hook = record.get("on_finish")
        if hook is not None:
            try:
                hook(query_id, status, reply)
            except Exception as exc:
                debug_log(f"webchat: a request hook failed: {type(exc).__name__}", "webchat")

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

    # -- attachments ------------------------------------------------------------------

    def add_upload(self, data: bytes) -> Dict[str, Any]:
        """Check and prepare an uploaded file by its content and hold it for the next request."""
        return self._held(classify(data))

    def capture_screenshot(self) -> Dict[str, Any]:
        """Capture the screen through the screenshot tool's own path and hold it for the next request."""
        return self._held(self._backend.capture_screenshot())

    def _held(self, attachment: RequestAttachment) -> Dict[str, Any]:
        return {"id": self._uploads.add(attachment), "kind": attachment.kind, "size": attachment.size}

    def discard_upload(self, upload_id: str) -> None:
        self._uploads.discard(upload_id)

    def attachment_support(self) -> Dict[str, Any]:
        """What attaching can do now: images, the Screenshot button, and the limits."""
        support = self._backend.attachment_support() or {}
        return {
            "images": support.get("images") or {"ok": False, "reason": "unavailable"},
            "screenshot": support.get("screenshot") or {"ok": False, "reason": "screenshot_unavailable"},
            "limits": {"max_upload_bytes": uploads.MAX_UPLOAD_BYTES, "max_attachments": MAX_ATTACHMENTS,
                       "max_images": MAX_IMAGES, "max_file_chars": MAX_FILE_CHARS},
        }

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

    def suggestions(self) -> Dict[str, Any]:
        """The start screen's cards. Never fails: a backend that cannot say gives the generic examples."""
        try:
            sources = self._backend.suggestion_sources()
            templates = suggestions.load_templates(self._backend.suggestion_languages())
        except Exception as exc:
            debug_log(f"webchat: start screen sources unavailable: {type(exc).__name__}", "webchat")
            sources, templates = {}, {}
        return {"cards": suggestions.build_cards(sources, templates)}

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
        newest = self._store.last_messages(active, 1) if chat is not None else []
        with self._cond:
            if newest:  # a notice older than a stored turn belongs to the past and goes
                self._notices = deque((n for n in self._notices if n["ts"] >= newest[0].ts), maxlen=MAX_NOTICES)
            notices = [dict(n) for n in self._notices]
            rev, library_rev, history_rev = self._rev, self._library_rev, self._history_rev
        return {
            "rev": rev,
            "library_rev": library_rev,
            "history_rev": history_rev,
            "ready": bool(self._backend.is_ready()),
            "state": self._effective_state(),
            "busy": bool(self._backend.is_busy()),
            "busy_query": self._typed_in_flight(),
            "active_chat_id": chat.id if chat else None,
            "chat": chat_dict(chat) if chat else None,
            "messages": [message_dict(m) for m in messages],
            "notices": notices,
            "live_steps": self._live_steps(),
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
