"""
Jarvis Voice Assistant Daemon

Main orchestrator that coordinates listening, reply generation, and output.
"""

from __future__ import annotations
import sys
import os
import time
import signal
import threading
import contextlib

# Fix OpenBLAS threading crash in bundled apps (must be before numpy imports)
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')

# Fix Windows console encoding for Unicode/emoji characters
# Skip in bundled mode (frozen) - encoding is handled by desktop_app.py
if sys.platform == 'win32' and not getattr(sys, 'frozen', False):
    try:
        import io
        # Only wrap the real console streams. Test harnesses (pytest) and
        # embedding code replace sys.stdout/sys.stderr with their own capture
        # objects; wrapping those detaches and closes their buffers, which
        # corrupts output capture for the rest of the process.
        if (sys.stdout is sys.__stdout__ and hasattr(sys.stdout, 'buffer')
                and hasattr(sys.stdout.buffer, 'write')):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        if (sys.stderr is sys.__stderr__ and hasattr(sys.stderr, 'buffer')
                and hasattr(sys.stderr.buffer, 'write')):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

from typing import Optional

from .config import load_settings
from .memory.db import Database
from .memory.conversation import DialogueMemory, update_diary_from_dialogue_memory
from .output.tts import create_tts_engine
from .tools.registry import (start_mcp_discovery, configure_windows_tools, configure_reply_mode_tool,
                             configure_activity_log_tool, configure_tv_tool, configure_screen_tool)
from .assistant_state import AssistantState, set_state
from .extensions import load_extensions
from .debug import debug_log
from .listening.listener import VoiceListener
from .utils.location import get_location_context, is_location_available

# Global instances for coordination between modules
_global_dialogue_memory: Optional[DialogueMemory] = None
_global_stop_requested: bool = False
_warm_profile_graph_listener = None  # registered callback, kept for shutdown unregister
_global_tts_engine = None  # TTS engine reference for face animation polling
_global_dictation_engine = None  # Dictation engine reference for history UI
# Config + DB booted by main(). Shared by the voice listener and the text-chat
# submission path so voice and text are one conversation against one store.
_global_cfg = None
_global_db = None

# Shutdown timeout for diary update (shorter than normal to allow reasonable quit time)
# Desktop app's stop_daemon() should wait at least this long + buffer
SHUTDOWN_DIARY_TIMEOUT_SEC = 45.0

# Callbacks for desktop app to receive diary update progress
# Set by desktop app before calling request_stop()
_diary_update_callbacks: dict = {
    "on_token": None,  # Callable[[str], None] - called for each LLM token
    "on_status": None,  # Callable[[str], None] - called for status updates
    "on_chunks": None,  # Callable[[List[str]], None] - called with pending chunks
    "on_complete": None,  # Callable[[bool], None] - called when done (success/fail)
}

# One query at a time: voice and text share this lock so they cannot race the
# dialogue memory. Held for the duration of a single reply-engine run.
_chat_query_lock = threading.Lock()

# Per-query cancellation flag for the text-chat path. Set by
# ``cancel_active_chat_query`` (the chat window's Stop button), checked by the
# chat worker after ``run_reply_engine`` returns so the reply is dropped
# instead of displayed. This is distinct from ``request_stop`` (daemon
# lifecycle shutdown) — cancelling a chat query must not tear down the voice
# assistant.
_chat_cancel_event: Optional[threading.Event] = None

# Chat IPC protocol prefixes - desktop app intercepts lines starting with these.
# __CHAT__:        daemon -> desktop (event stream, mirrors DIARY_IPC_PREFIX)
# __CHAT_QUERY__:  desktop -> daemon (query submission, read from stdin)
CHAT_IPC_PREFIX = "__CHAT__:"
CHAT_QUERY_IPC_PREFIX = "__CHAT_QUERY__:"
# Cancellation travels the same way a submission does. In subprocess
# mode the query runs here, in the daemon, whose module globals are a
# different instance from the desktop app's: calling the cancel
# function over there sets a flag nobody in this process reads.
CHAT_CANCEL_IPC_PREFIX = "__CHAT_CANCEL__"
# Session control (subprocess mode): new session (bare line), rewind to a
# typed message and regenerate its reply, and restore an archived session.
# All operate on the daemon's shared dialogue memory, which is where the
# conversation actually lives.
CHAT_NEW_SESSION_IPC_PREFIX = "__CHAT_NEW_SESSION__"
CHAT_REWIND_IPC_PREFIX = "__CHAT_REWIND__:"
CHAT_RESTORE_IPC_PREFIX = "__CHAT_RESTORE__:"
# Tray click (subprocess mode): wake as if the wake word were spoken.
WAKE_IPC_PREFIX = "__WAKE__"
# Reply mode (subprocess mode): the tray asks for a switch with ``{"mode": ...}``; the daemon reports
# the active and allowed modes with a ``state`` event after start-up and after every switch.
REPLY_MODE_IPC_PREFIX = "__REPLY_MODE__:"
REPLY_MODE_EVENT_PREFIX = "__REPLY_MODE_STATE__:"
# Activity log (subprocess mode): the tray asks to pause, resume or delete the history with
# ``{"action": "pause" | "resume" | "delete"}``.
ACTIVITY_IPC_PREFIX = "__ACTIVITY__:"

# The running voice listener, so a wake request can reach it.
_global_voice_listener = None


def request_stop() -> None:
    """Request the daemon to stop gracefully."""
    global _global_stop_requested
    _global_stop_requested = True


def set_diary_update_callbacks(
    on_token=None,
    on_status=None,
    on_chunks=None,
    on_complete=None,
) -> None:
    """
    Set callbacks for diary update progress during shutdown.

    These are used by the desktop app to show a live diary update dialog.

    Args:
        on_token: Called with each LLM token as it's generated
        on_status: Called with status messages
        on_chunks: Called with the list of pending conversation chunks
        on_complete: Called when diary update completes (bool = success)
    """
    global _diary_update_callbacks
    _diary_update_callbacks["on_token"] = on_token
    _diary_update_callbacks["on_status"] = on_status
    _diary_update_callbacks["on_chunks"] = on_chunks
    _diary_update_callbacks["on_complete"] = on_complete


def get_pending_diary_chunks() -> list:
    """Get pending conversation chunks from dialogue memory (for UI display only).

    Uses ``get_pending_chunks()`` which discards the atomic snapshot timestamp.
    Do not use the result of this function to drive diary saves — the actual
    save path goes through ``update_diary_from_dialogue_memory``, which calls
    ``get_pending_chunks_with_snapshot()`` internally.
    """
    global _global_dialogue_memory
    if _global_dialogue_memory is None:
        return []
    return _global_dialogue_memory.get_pending_chunks()


def get_hot_window_messages() -> list:
    """Return the current hot-window turns (last ``RECENT_WINDOW_SEC``) as
    ``[{"role": "user"|"assistant", "content": str}, ...]``.

    The chat window seeds its transcript from this on first open so a user who
    has been talking by voice sees recent turns instead of a blank panel. The
    content is already redacted (redaction runs before a turn is added to the
    dialogue memory), so this never leaks raw sensitive input. Returns an empty
    list when the daemon has not booted or the hot window is empty.
    """
    if _global_dialogue_memory is None:
        return []
    return _global_dialogue_memory.get_recent_messages()


def get_dialogue_memory():
    """The shared dialogue memory, or ``None`` before the daemon has booted (phone access mirrors it)."""
    return _global_dialogue_memory


def is_query_running() -> bool:
    """True while a voice or text query holds the reply engine."""
    return _chat_query_lock.locked()


def new_chat_session() -> bool:
    """Start a fresh conversation: clear the shared dialogue memory.

    Voice and text share this memory, so a new session resets both. The
    previous conversation is not lost anywhere else — the chat window
    keeps an in-memory archive of its transcript for the session list.
    Nothing is written to disk. Returns False when a query is currently
    running (the engine appends its turns after we clear, resurrecting
    the conversation); the caller should retry after the query finishes.
    """
    global _global_dialogue_memory
    if _global_dialogue_memory is None:
        return False
    if not _chat_query_lock.acquire(blocking=False):
        debug_log("new chat session rejected: a query is in flight", "chat")
        return False
    try:
        _global_dialogue_memory.clear()
        return True
    finally:
        _chat_query_lock.release()


def set_chat_messages(messages: list) -> bool:
    """Restore an archived session into the shared dialogue memory.

    Used when the chat window switches back to a session from its
    in-memory list. Redaction is applied here, on the daemon side, so the
    diary (written at session end from this memory) never sees raw user
    text even if the window's archive holds it. Returns False when a
    query is currently running (see ``new_chat_session``).
    """
    global _global_dialogue_memory
    if _global_dialogue_memory is None:
        return False
    if not _chat_query_lock.acquire(blocking=False):
        debug_log("chat restore rejected: a query is in flight", "chat")
        return False
    try:
        from .utils.redact import redact

        scrubbed = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if isinstance(role, str) and isinstance(content, str) and role in ("user", "assistant"):
                scrubbed.append({"role": role, "content": redact(content)})
        _global_dialogue_memory.set_messages(scrubbed)
        return True
    finally:
        _chat_query_lock.release()


# Diary IPC protocol prefix - desktop app intercepts lines starting with this
DIARY_IPC_PREFIX = "__DIARY__:"


def _emit_ipc_event(prefix: str, event_type: str, data, debug_tag: str) -> None:
    """Emit a JSON IPC event line to stdout for the desktop app (subprocess mode).

    Shared by the diary and chat event emitters. Builds ``{"type", "data"}``,
    prints ``{prefix}{json}`` with flush, skips debug logging for ``token``
    events to avoid spam, and swallows+logs emit errors so a bad payload never
    crashes the worker.
    """
    import json
    try:
        event = {"type": event_type, "data": data}
        line = f"{prefix}{json.dumps(event)}"
        print(line, flush=True)
        if event_type != "token":  # Don't spam for tokens
            debug_log(f"IPC event emitted: {event_type}", debug_tag)
    except Exception as e:
        debug_log(f"IPC emit error: {e}", debug_tag)


def _emit_diary_event(event_type: str, data) -> None:
    """Emit a diary update event to stdout for IPC with the desktop app.

    Used in subprocess mode where callbacks aren't available. The desktop app
    intercepts these lines and forwards them to the diary dialog.

    Args:
        event_type: One of "chunks", "token", "status", "complete"
        data: Event payload (list for chunks, str for token/status, bool for complete)
    """
    _emit_ipc_event(DIARY_IPC_PREFIX, event_type, data, "diary_ipc")


def _emit_chat_event(event_type: str, data) -> None:
    """Emit a chat event to stdout for IPC with the desktop app (subprocess mode).

    The payload never carries unredacted user text: the caller passes the
    already-redacted query to the ``start`` event.
    """
    _emit_ipc_event(CHAT_IPC_PREFIX, event_type, data, "chat_ipc")


def _notify_chat(event_type: str, data, *, callbacks: dict, use_ipc: bool) -> None:
    """Dispatch a chat event to per-call callbacks and/or the IPC stream.

    ``callbacks`` is the dict of caller-supplied callables (``on_start`` etc.),
    not a module global. ``busy`` takes no argument; all others take ``data``.
    """
    callback_map = {
        "rewind": "on_rewind",
        "start": "on_start",
        "token": "on_token",
        "tool": "on_tool_call",
        "complete": "on_complete",
        "busy": "on_busy",
    }
    callback_name = callback_map.get(event_type)
    if callbacks and callback_name:
        cb = callbacks.get(callback_name)
        if cb is not None:
            try:
                if event_type == "busy":
                    cb()
                else:
                    cb(data)
            except Exception:
                pass
    if use_ipc:
        _emit_chat_event(event_type, data)


_chat_result_callback = None
_chat_result_callback_lock = threading.Lock()


def set_chat_result_callback(callback) -> None:
    """Register the chat UI's receiver for confirmed-action results.

    ``callback(reply: str)`` fires from the confirmed-action worker thread, so
    the desktop app passes a Qt signal emitter and the UI update happens on the
    main thread. ``None`` unregisters.
    """
    global _chat_result_callback
    with _chat_result_callback_lock:
        _chat_result_callback = callback


def deliver_chat_confirmed_result(reply: str, success: bool) -> None:
    """Report the outcome of a confirmed action that text chat requested.

    The redacted outcome joins the shared conversation (so follow-up questions
    see it) and is forwarded to the chat UI. It is never spoken.
    """
    from .utils.redact import redact
    safe_reply = redact(reply)
    dm = _global_dialogue_memory
    if dm is not None:
        try:
            dm.add_message("assistant", safe_reply)
        except Exception as exc:
            debug_log(f"recording confirmed chat result failed: {exc}", "chat")
    with _chat_result_callback_lock:
        callback = _chat_result_callback
    if callback is not None:
        try:
            callback(safe_reply)
        except Exception as exc:
            # e.g. the window's signal bridge was destroyed during shutdown
            debug_log(f"chat result delivery failed: {exc}", "chat")


def install_chat_result_delivery() -> None:
    """Route chat-origin confirmed-action results to ``deliver_chat_confirmed_result``."""
    from .tools.confirmation import ORIGIN_CHAT, set_result_handler
    set_result_handler(deliver_chat_confirmed_result, origin=ORIGIN_CHAT)


@contextlib.contextmanager
def query_lock():
    """Context manager that acquires the shared voice+text query lock (blocking).

    Used by the voice path so a voice query waits for any in-flight text query
    to finish before running ``run_reply_engine`` against the shared dialogue
    memory. The text path uses a non-blocking acquire (reject-with-busy) in
    ``submit_text_query``; the voice path blocks because voice queries should
    not be silently dropped when text is running.
    """
    _chat_query_lock.acquire()
    try:
        yield
    finally:
        _chat_query_lock.release()


def cancel_active_chat_query() -> None:
    """Cancel the in-flight text-chat query, if any.

    Sets the per-query cancellation flag so the chat worker drops the reply
    when ``run_reply_engine`` returns. Distinct from ``request_stop`` (full
    daemon shutdown): this does not stop the voice listener, save the diary,
    or close the database. Used by the chat window's Stop button.
    """
    global _chat_cancel_event
    if _chat_cancel_event is not None:
        _chat_cancel_event.set()
        debug_log("chat query cancellation requested", "chat")
    from .bridge import runtime as bridge_runtime
    bridge_runtime.cancel_active_request("stop")
    from .routines import runner as routine_runner
    routine_runner.stop_running()


def submit_text_query(
    text: str,
    *,
    on_start=None,
    on_token=None,
    on_tool_call=None,
    on_complete=None,
    on_busy=None,
    use_ipc: bool = False,
    origin: str = "chat",
) -> None:
    """Submit a text query to the reply engine (fire-and-forget).

    Runs ``run_reply_engine`` on a worker thread with ``tts=None`` and the
    shared global dialogue memory, so text and voice are one conversation.
    Results are delivered via the per-call callbacks (bundled mode) and/or
    ``__CHAT__:`` IPC events (subprocess mode). See ``chat_window.spec.md``.

    A second submission while one is running is rejected with a ``busy``
    event rather than queued. A running query can be cancelled with
    ``cancel_active_chat_query``; the reply is then dropped (``complete(None)``)
    rather than displayed.

    ``origin`` is ``chat`` for the desktop chat window and ``phone`` for a
    paired phone, which never takes the window in front of the PC as what the
    request is about (``memory/desktop_referents.spec.md``).
    """
    if not text or not text.strip():
        return

    callbacks = {
        "on_start": on_start,
        "on_token": on_token,
        "on_tool_call": on_tool_call,
        "on_complete": on_complete,
        "on_busy": on_busy,
    }

    dm = _global_dialogue_memory
    cfg = _global_cfg
    db = _global_db
    if dm is None or cfg is None or db is None:
        # Daemon not initialised (e.g. tests that don't boot main()). Fail
        # open with a None complete so the UI doesn't hang.
        _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)
        return

    if is_stop_requested():
        # Daemon is shutting down. Don't spawn a worker that may use db after
        # close (bundled) or write to a dead pipe (subprocess).
        _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)
        return

    # One query at a time: voice and text share the lock.
    if not _chat_query_lock.acquire(blocking=False):
        _notify_chat("busy", None, callbacks=callbacks, use_ipc=use_ipc)
        debug_log("chat query rejected: another query is running", "chat")
        return

    _start_text_query_worker(text, dm, cfg, db, callbacks=callbacks, use_ipc=use_ipc, origin=origin)


def regenerate_chat_reply(
    text: str,
    *,
    occurrence: int = 0,
    on_rewind=None,
    on_start=None,
    on_token=None,
    on_tool_call=None,
    on_complete=None,
    on_busy=None,
    use_ipc: bool = False,
) -> None:
    """Rewind the conversation to before a typed message and ask it again (chat Rewind).

    The message is found in the shared dialogue memory by its redacted
    content (or as given, for a turn the window seeded already redacted),
    ``occurrence`` counting back from the latest user turn with that content
    (``DialogueMemory.rewind_before_user_text``). The turn and
    everything after it are dropped, then ``text`` is run again as a fresh
    query, all under one hold of the query lock so no other query can slip
    in between. A ``rewind`` event reports first whether the turn was found:
    ``True`` is followed by the usual ``start`` / ``complete`` events, and
    ``False`` (the turn is no longer in memory, or the daemon is not
    running) changes nothing. A query already running answers ``busy``.
    """
    callbacks = {
        "on_rewind": on_rewind,
        "on_start": on_start,
        "on_token": on_token,
        "on_tool_call": on_tool_call,
        "on_complete": on_complete,
        "on_busy": on_busy,
    }
    dm = _global_dialogue_memory
    cfg = _global_cfg
    db = _global_db
    if not text or not text.strip() or dm is None or cfg is None or db is None or is_stop_requested():
        _notify_chat("rewind", False, callbacks=callbacks, use_ipc=use_ipc)
        return
    if not _chat_query_lock.acquire(blocking=False):
        _notify_chat("busy", None, callbacks=callbacks, use_ipc=use_ipc)
        debug_log("chat rewind rejected: a query is in flight", "chat")
        return
    try:
        from .utils.redact import redact
        # Memory holds turns redacted. A typed message is matched by its
        # redaction; one the window seeded from the hot window is shown as
        # stored, already redacted, and redaction is not idempotent.
        rewound = (dm.rewind_before_user_text(redact(text), occurrence)
                   or dm.rewind_before_user_text(text, occurrence))
    except Exception as exc:
        debug_log(f"chat rewind failed: {exc}", "chat")
        rewound = False
    _notify_chat("rewind", rewound, callbacks=callbacks, use_ipc=use_ipc)
    if not rewound:
        debug_log("chat rewind: the message is no longer in memory", "chat")
        _chat_query_lock.release()
        return
    debug_log("chat rewind applied, regenerating the reply", "chat")
    _start_text_query_worker(text, dm, cfg, db, callbacks=callbacks, use_ipc=use_ipc, origin="chat")


def _start_text_query_worker(text: str, dm, cfg, db, *, callbacks: dict, use_ipc: bool, origin: str) -> None:
    """Run one text query on a worker thread. The caller holds ``_chat_query_lock``; the worker releases it."""
    # Per-query cancellation flag. The Stop button sets this so the worker
    # drops the reply instead of displaying it.
    global _chat_cancel_event
    cancel_event = threading.Event()
    _chat_cancel_event = cancel_event

    def _worker() -> None:
        try:
            # Snapshot the redacted query for the start event. ``run_reply_engine``
            # redacts internally too; we mirror that here so the IPC stream and
            # the UI never carry raw user text even if the engine hasn't run yet.
            # Done inside the worker's try/except so a redaction failure fails
            # open (complete(None)) and the shared lock is released in finally
            # rather than leaking and blocking every future submission.
            from .utils.redact import redact
            display_query = redact(text)
            _notify_chat("start", display_query, callbacks=callbacks, use_ipc=use_ipc)
            from .reply.engine import run_reply_engine
            reply = run_reply_engine(
                db=db,
                cfg=cfg,
                tts=None,
                text=text,
                dialogue_memory=dm,
                language=None,
                quiet=True,
                origin=origin,
            )
            if cancel_event.is_set():
                debug_log("chat query cancelled, dropping reply", "chat")
                reply = None
            _notify_chat("complete", reply, callbacks=callbacks, use_ipc=use_ipc)
        except Exception as exc:
            debug_log(f"chat query worker error: {exc}", "chat")
            try:
                _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)
            except Exception:
                pass
        finally:
            global _chat_cancel_event
            if _chat_cancel_event is cancel_event:
                _chat_cancel_event = None
            _chat_query_lock.release()

    try:
        threading.Thread(target=_worker, name="jarvis-chat-query", daemon=True).start()
    except Exception:
        # If thread spawning fails, release the lock so future queries work.
        _chat_cancel_event = None
        _chat_query_lock.release()
        _notify_chat("complete", None, callbacks=callbacks, use_ipc=use_ipc)


def handle_chat_query_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat-query submission (subprocess mode).

    Returns True if the line was a ``__CHAT_QUERY__:`` line and was handled
    (whether or not the query was accepted). Returns False for any other
    line, so the caller can still apply SHUTDOWN / EOF semantics.
    """
    line = line.strip()
    if not line.startswith(CHAT_QUERY_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(CHAT_QUERY_IPC_PREFIX):])
        text = payload.get("text", "")
    except Exception:
        debug_log("malformed __CHAT_QUERY__ line ignored", "chat_ipc")
        return True
    # Reject non-string payloads (e.g. {"text":[1]}) so a hostile or buggy
    # writer can't crash the stdin monitor via submit_text_query's str API.
    if not isinstance(text, str):
        debug_log("__CHAT_QUERY__ text payload is not a string, ignored", "chat_ipc")
        return True
    # In subprocess mode the reply comes back via __CHAT__: events on stdout.
    submit_text_query(text, use_ipc=True)
    return True


def handle_chat_cancel_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat-query cancellation (subprocess mode).

    Returns True when the line was a cancel instruction and was handled,
    False for anything else so the caller can apply its own semantics.
    Cancelling with nothing in flight is a no-op, not an error: the user
    can press Stop after the engine has already returned.
    """
    if line.strip() != CHAT_CANCEL_IPC_PREFIX:
        return False
    cancel_active_chat_query()
    return True


def toggle_manual_wake() -> None:
    """Orb click: stop whatever Jarvis is doing, or wake him as if the wake word were spoken alone.

    A typed request in flight is cancelled instead of waking. A no-op while no listener is running.
    """
    if _chat_cancel_event is not None:
        debug_log("manual wake: stopping the typed request in progress", "jarvis")
        cancel_active_chat_query()
        return
    listener = _global_voice_listener
    if listener is None:
        debug_log("manual wake ignored: no voice listener running", "jarvis")
        return
    listener.toggle_manual_wake()


def set_reply_mode(mode: str):
    """Switch the reply mode at runtime (tray, bundled mode). Returns the ``SwitchResult``.

    Only modes allowed in Settings are accepted. A request in flight is cancelled by the switch.
    """
    from .bridge import modes
    return modes.switch(mode)


def handle_reply_mode_stdin_line(line: str) -> bool:
    """Parse a stdin line as a reply-mode switch request (subprocess mode).

    Returns True when the line was a reply-mode request, False for anything else.
    """
    if not line.startswith(REPLY_MODE_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(REPLY_MODE_IPC_PREFIX):])
        mode = payload.get("mode") if isinstance(payload, dict) else None
    except ValueError:
        mode = None
    if not isinstance(mode, str):
        debug_log("malformed reply mode request ignored", "jarvis")
        return True
    # The switch closes and starts child processes, so it never runs on the stdin reader.
    threading.Thread(target=set_reply_mode, args=(mode,), daemon=True, name="reply-mode-switch").start()
    return True


def set_activity_paused(paused: bool) -> bool:
    """Pause or resume the opt-in activity log (tray, bundled mode) and remember the choice."""
    from .memory import activity_runtime
    return activity_runtime.set_paused(paused)


def stop_activity_cloud_sharing() -> None:
    """Withhold the activity log from Codex and Claude now (Settings switched sharing off)."""
    from .memory import activity_runtime
    activity_runtime.stop_cloud_sharing()


def delete_activity_history() -> int:
    """Delete every recorded activity session (tray, bundled mode). Returns how many were removed."""
    from .memory import activity_runtime
    return activity_runtime.delete_history()


def handle_activity_stdin_line(line: str) -> bool:
    """Parse a stdin line as an activity log request (subprocess mode).

    Returns True when the line was an activity request (even a malformed one), False for anything else.
    """
    if not line.startswith(ACTIVITY_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(ACTIVITY_IPC_PREFIX):])
        action = payload.get("action") if isinstance(payload, dict) else None
    except ValueError:
        action = None
    handlers = {"pause": lambda: set_activity_paused(True), "resume": lambda: set_activity_paused(False),
                "delete": delete_activity_history, "stop_sharing": stop_activity_cloud_sharing}
    if action not in handlers:
        debug_log("malformed activity request ignored", "jarvis")
        return True
    # Deleting touches the database, so it never runs on the stdin reader.
    threading.Thread(target=handlers[action], daemon=True, name="activity-request").start()
    return True


def _emit_reply_mode_state(mode: str, enabled: list) -> None:
    _emit_ipc_event(REPLY_MODE_EVENT_PREFIX, "state", {"mode": mode, "enabled": enabled}, "reply_mode")


def handle_wake_stdin_line(line: str) -> bool:
    """Parse a stdin line as a manual wake request (subprocess mode).

    Returns True when the line was a wake request, False for anything else.
    """
    if line.strip() != WAKE_IPC_PREFIX:
        return False
    toggle_manual_wake()
    return True


def handle_chat_new_session_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat new-session instruction (subprocess mode).

    Returns True when the line was the new-session instruction and was
    handled, False otherwise so the caller can apply its own semantics.
    """
    if line.strip() != CHAT_NEW_SESSION_IPC_PREFIX:
        return False
    new_chat_session()
    return True


def handle_chat_rewind_stdin_line(line: str) -> bool:
    """Parse a stdin line as a chat rewind instruction (subprocess mode).

    Payload is ``{"text": "<the message>", "occurrence": N}`` (``occurrence``
    optional, 0 by default; see ``regenerate_chat_reply``). The outcome comes
    back as ``__CHAT__:`` events. Returns True when the line was a rewind
    instruction and was handled (whether or not a rewind actually happened),
    False for anything else.
    """
    line = line.strip()
    if not line.startswith(CHAT_REWIND_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(CHAT_REWIND_IPC_PREFIX):])
        text = payload.get("text")
        occurrence = payload.get("occurrence", 0)
    except Exception:
        debug_log("malformed __CHAT_REWIND__ line ignored", "chat_ipc")
        return True
    if not isinstance(text, str) or not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 0:
        debug_log("__CHAT_REWIND__ payload not usable, ignored", "chat_ipc")
        return True
    regenerate_chat_reply(text, occurrence=occurrence, use_ipc=True)
    return True


def handle_chat_restore_stdin_line(line: str) -> bool:
    """Parse a stdin line as a session-restore instruction (subprocess mode).

    Payload is ``{"messages": [{"role", "content"}, ...]}``. Returns True
    when the line was a restore instruction and was handled (even if the
    payload was malformed), False for anything else.
    """
    line = line.strip()
    if not line.startswith(CHAT_RESTORE_IPC_PREFIX):
        return False
    import json
    try:
        payload = json.loads(line[len(CHAT_RESTORE_IPC_PREFIX):])
        messages = payload.get("messages", [])
    except Exception:
        debug_log("malformed __CHAT_RESTORE__ line ignored", "chat_ipc")
        return True
    if not isinstance(messages, list):
        debug_log("__CHAT_RESTORE__ messages not a list, ignored", "chat_ipc")
        return True
    set_chat_messages(messages)
    return True


def wait_for_chat_worker(timeout_sec: float = 5.0) -> bool:
    """Wait for an in-flight chat worker to finish, bounded.

    Shutdown runs the final diary pass and closes the database. A worker
    that started just before the stop request is still inside
    ``run_reply_engine`` with that connection open, so closing it under
    them raises on a closed SQLite handle, and the diary pass races their
    writes to dialogue memory.

    Returns True when the worker finished (or none was running), False on
    timeout — in which case the caller proceeds anyway rather than
    hanging the quit, which is the lesser of the two failures.
    """
    acquired = _chat_query_lock.acquire(timeout=timeout_sec)
    if acquired:
        _chat_query_lock.release()
        return True
    debug_log(
        f"chat worker still running after {timeout_sec}s, shutting down anyway",
        "chat",
    )
    return False


def is_stop_requested() -> bool:
    """Check if a stop has been requested."""
    return _global_stop_requested


def get_tts_engine():
    """Get the global TTS engine for speaking state polling (used by face widget)."""
    return _global_tts_engine


def get_dictation_engine():
    """Get the global dictation engine (used by desktop app for history window)."""
    return _global_dictation_engine


def _stdin_ipc_enabled() -> bool:
    """Whether this daemon reads its stdin for IPC.

    On Windows (shutdown signal) and whenever the desktop app explicitly signals it owns our stdin
    (subprocess chat query-in on any platform). The desktop app sets ``JARVIS_STDIN_IPC=1`` when
    spawning us so that a bare ``python -m jarvis.main < /dev/null`` (or a systemd unit with
    StandardInput=null) does NOT start the monitor and immediately exit on EOF. Bundled mode uses a
    QThread, not a subprocess, so it's skipped.
    """
    if getattr(sys, 'frozen', False):
        return False
    return sys.platform == "win32" or os.environ.get("JARVIS_STDIN_IPC") == "1"


def _install_signal_handlers() -> None:
    """Ensure signals like Ctrl+Break trigger clean shutdown."""
    def _raise_keyboard_interrupt(_signum, _frame):
        raise KeyboardInterrupt()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _raise_keyboard_interrupt)
            except Exception:
                pass


def _check_and_update_diary(
    db: Database, cfg, verbose: bool = False, force: bool = False, timeout_sec: Optional[float] = None,
    use_callbacks: bool = False, use_ipc: bool = False
) -> None:
    """Check if diary should be updated and perform batch update if needed.

    Args:
        timeout_sec: Optional override for LLM timeout. If None, uses cfg.llm_chat_timeout_sec.
                    During shutdown, a shorter timeout is used to allow graceful quit.
        use_callbacks: If True, uses the global diary update callbacks for UI updates.
        use_ipc: If True, emits diary events to stdout for IPC with desktop app (subprocess mode).
    """
    global _global_dialogue_memory, _diary_update_callbacks

    debug_log(f"diary update check: force={force}, verbose={verbose}", "memory")

    # Helper to safely call callbacks and/or emit IPC events
    def _notify(event_type: str, data):
        # Map event types to callback names
        callback_map = {"chunks": "on_chunks", "status": "on_status", "token": "on_token", "complete": "on_complete"}
        callback_name = callback_map.get(event_type)

        # Call callback if set (bundled mode)
        if use_callbacks and callback_name and _diary_update_callbacks.get(callback_name):
            try:
                _diary_update_callbacks[callback_name](data)
            except Exception:
                pass

        # Emit IPC event (subprocess mode)
        if use_ipc:
            _emit_diary_event(event_type, data)

    if _global_dialogue_memory is None:
        debug_log("diary update skipped: dialogue_memory is None", "memory")
        _notify("complete", False)
        return

    try:
        should_update = force or _global_dialogue_memory.should_update_diary()
        debug_log(f"diary update: should_update={should_update}, force={force}", "memory")

        if should_update:
            # Display-only: get a snapshot of pending chunks to notify the UI.
            # The atomic snapshot for the actual save is captured inside
            # update_diary_from_dialogue_memory via get_pending_chunks_with_snapshot().
            pending_chunks = _global_dialogue_memory.get_pending_chunks()
            debug_log(f"diary update: found {len(pending_chunks)} pending chunks", "memory")

            if not pending_chunks:
                debug_log("diary update skipped: no pending chunks", "memory")
                _notify("complete", False)
                return

            # Notify about chunks and status
            _notify("chunks", pending_chunks)
            _notify("status", "Writing diary entry...")

            if verbose:
                try:
                    print("📝 Updating your diary. Please wait… (don't press Ctrl+C again)", file=sys.stderr, flush=True)
                except Exception:
                    pass

            source_app = "stdin" if cfg.use_stdin else "voice"
            effective_timeout = timeout_sec if timeout_sec is not None else cfg.llm_chat_timeout_sec

            # Create token handler that notifies via callback and/or IPC
            # For IPC mode, batch tokens to avoid overwhelming the receiver
            token_buffer = []
            last_flush_time = [time.time()]  # Use list for closure mutability
            TOKEN_FLUSH_INTERVAL = 0.1  # Flush every 100ms

            def on_token_handler(token: str):
                if use_callbacks:
                    # Callbacks can handle individual tokens (same process)
                    _notify("token", token)
                elif use_ipc:
                    # IPC mode: batch tokens to reduce event frequency
                    token_buffer.append(token)
                    now = time.time()
                    if now - last_flush_time[0] >= TOKEN_FLUSH_INTERVAL:
                        if token_buffer:
                            _emit_diary_event("token", "".join(token_buffer))
                            token_buffer.clear()
                        last_flush_time[0] = now

            # Only use token handler if we have callbacks or IPC enabled
            on_token = on_token_handler if (use_callbacks or use_ipc) else None

            # Graph best-child picker is a one-digit classification — a fast-tier
            # job, so placement runs on the small model instead of paging in the
            # big chat model for every fact.
            from .llm import resolve_model, Tier
            graph_picker_model = resolve_model(cfg, Tier.FAST)

            summary_id = update_diary_from_dialogue_memory(
                db=db,
                dialogue_memory=_global_dialogue_memory,
                cfg=cfg,
                source_app=source_app,
                voice_debug=cfg.voice_debug,
                timeout_sec=effective_timeout,
                force=force,
                on_token=on_token,
                thinking=getattr(cfg, 'llm_thinking_enabled', False),
                graph_picker_model=graph_picker_model,
            )

            # Flush any remaining tokens in IPC mode
            if use_ipc and token_buffer:
                _emit_diary_event("token", "".join(token_buffer))
                token_buffer.clear()

            if summary_id:
                debug_log(f"diary updated from dialogue memory: id={summary_id}", "memory")
                _notify("complete", True)
            else:
                debug_log("diary update from dialogue memory failed", "memory")
                _notify("complete", False)

            if verbose:
                try:
                    if summary_id:
                        print("✅ Diary update finished.", file=sys.stderr, flush=True)
                    else:
                        print("⚠️ Diary update failed. Shutting down anyway.", file=sys.stderr, flush=True)
                except Exception:
                    pass
        else:
            # No update needed
            _notify("complete", False)
    except Exception as e:
        debug_log(f"diary update check error: {e}", "memory")
        _notify("complete", False)


def _report_mcp_discovery(mcps: dict, tools: dict, errors: dict) -> None:
    """Print what each configured MCP server offers once background discovery is done."""
    counts: dict = {}
    for tool_name in tools:
        if "__" in tool_name:
            server_name = tool_name.split("__")[0]
            counts[server_name] = counts.get(server_name, 0) + 1
    print("📡 MCP tools discovered:", flush=True)
    for server_name in mcps:
        count = counts.get(server_name, 0)
        if count > 0:
            print(f"  ✅ {server_name}: {count} tools available", flush=True)
        elif server_name in errors:
            print(f"  ❌ {server_name}: {errors[server_name]}", flush=True)
        else:
            print(f"  ⚠️ {server_name}: no tools discovered", flush=True)
    if "_global" in errors:
        print(f"  ⚠️ MCP discovery failed: {errors['_global']}", flush=True)


def main(smoke_test: bool = False) -> None:
    """Main daemon entry point.

    Args:
        smoke_test: If True, initialise all components, print a success
            marker, and return without entering the main event loop.
            Used by CI smoke tests to verify the build is not broken.
    """
    global _global_dialogue_memory, _global_stop_requested, _global_tts_engine, _global_dictation_engine
    global _warm_profile_graph_listener, _global_voice_listener

    # Reset stop flag at start (in case of restart)
    _global_stop_requested = False

    _install_signal_handlers()

    cfg = load_settings()
    configure_windows_tools(cfg)
    configure_reply_mode_tool(cfg)
    configure_activity_log_tool(cfg)
    configure_screen_tool(cfg)
    configure_tv_tool(cfg)
    # Local extensions (extensions/extensions.spec.md): after the built-in tools, before the voice engine.
    extensions = load_extensions(cfg)
    extensions.install()
    extensions.start()
    db = Database(cfg.db_path, cfg.sqlite_vss_path)
    # Expose cfg + db so the text-chat submission path shares the same store
    # and config as the voice listener (one conversation, one config).
    global _global_cfg, _global_db
    _global_cfg = cfg
    _global_db = db

    debug_log("daemon started", "jarvis")
    print("✓ Daemon started", flush=True)
    print(f"🧠 Using chat model: {cfg.llm_chat_model}", flush=True)
    print(f"🎤 Using whisper model: {cfg.whisper_model}", flush=True)

    # MCP preflight: servers start and list their tools in the background while the rest of
    # start-up (TTS, Whisper, model warm-ups) goes on; requests that need tools wait for it.
    mcps = getattr(cfg, "mcps", {}) or {}
    mcp_discovery: Optional[threading.Thread] = None
    if mcps:
        print(f"📡 Discovering MCP tools from {len(mcps)} server(s) in the background...", flush=True)
        mcp_discovery = start_mcp_discovery(
            mcps, on_done=lambda tools, errors: _report_mcp_discovery(mcps, tools, errors))
    else:
        print("📡 No MCP servers configured", flush=True)

    # Initialize dialogue memory with timeout
    print("💾 Initializing dialogue memory...", flush=True)
    _global_dialogue_memory = DialogueMemory(
        inactivity_timeout=cfg.dialogue_memory_timeout,
        max_interactions=20
    )
    print("✓ Dialogue memory initialized", flush=True)
    install_chat_result_delivery()

    # Reply mode: local starts nothing; an allowed cloud mode starts its background bridge.
    from .bridge import modes as reply_modes
    if os.environ.get("JARVIS_STDIN_IPC") == "1":
        # The desktop app owns our stdout: it shows the mode in the tray and on the orb.
        reply_modes.add_listener(_emit_reply_mode_state)
    reply_modes.start(cfg)

    # Opt-in activity log: starts nothing unless the user turned it on in Settings.
    from .memory import activity_runtime
    activity_runtime.start(cfg)

    # Wire the conversation-scoped warm-profile cache to graph mutations.
    # When the User or Directives branch is mutated mid-conversation, the
    # cached warm profile is dropped so the next reply rebuilds it from
    # the current graph state. World-branch writes (typical webSearch
    # extractions) do not touch warm profile, so they are ignored.
    try:
        from .memory.graph import (
            BRANCH_DIRECTIVES,
            BRANCH_USER,
            register_graph_mutation_listener,
        )

        _wp_relevant_branches = {BRANCH_USER, BRANCH_DIRECTIVES}

        # Read the DialogueMemory ref through the module global at fire
        # time, not via closure capture, so a future singleton swap (tests
        # or hot-reload) routes invalidation to the live instance instead
        # of the freed one.
        def _invalidate_wp_on_graph_mutation(*, action, node_id, branch):
            del action, node_id  # Only the branch matters for warm-profile filtering.
            if branch not in _wp_relevant_branches:
                return
            dm = _global_dialogue_memory
            if dm is None:
                return
            try:
                dm.invalidate_warm_profile()
                debug_log(
                    f"warm profile invalidated by {branch} graph mutation",
                    "memory",
                )
            except Exception as exc:
                debug_log(
                    f"warm profile invalidation failed (non-fatal): {exc}",
                    "memory",
                )

        # If a previous run left a listener registered (re-entry without
        # full process restart), drop it before installing the new one so
        # the registry never accumulates stale closures.
        if _warm_profile_graph_listener is not None:
            try:
                from .memory.graph import unregister_graph_mutation_listener
                unregister_graph_mutation_listener(_warm_profile_graph_listener)
            except Exception:
                pass
        register_graph_mutation_listener(_invalidate_wp_on_graph_mutation)
        _warm_profile_graph_listener = _invalidate_wp_on_graph_mutation
    except Exception as exc:
        debug_log(
            f"warm profile mutation listener wiring failed (non-fatal): {exc}",
            "memory",
        )

    # Knowledge graph: wipe + re-seed if the on-disk shape predates the
    # User/Directives/World taxonomy. Non-destructive to the diary —
    # users can re-import via the memory viewer.
    try:
        from .memory.graph import GraphMemoryStore
        _graph_store_boot = GraphMemoryStore(cfg.db_path)
        if _graph_store_boot.migrate_legacy_shape():
            print("🧹 Wiped legacy knowledge graph; re-seeded User / Directives / World branches", flush=True)
            print("   📥 Open the memory viewer and use 'Import from Diary' to repopulate.", flush=True)
        _graph_store_boot.close()
    except Exception as e:
        debug_log(f"graph legacy-shape migration failed (non-fatal): {e}", "memory")

    # Check location detection status
    if cfg.location_enabled and not is_location_available():
        print("⚠️ 📍 Optional location features unavailable. Add a GeoLite2 database in Setup → Location.", flush=True)
    elif cfg.location_enabled:
        location_context = get_location_context(
            config_ip=cfg.location_ip_address,
            auto_detect=cfg.location_auto_detect,
            resolve_cgnat_public_ip=cfg.location_cgnat_resolve_public_ip,
            location_cache_minutes=cfg.location_cache_minutes,
        )
        if location_context == "Location: Unknown":
            print("📍 Location detection not available", flush=True)
            if not is_location_available():
                print("     GeoLite2 database not found. Download from:", flush=True)
                print("     https://www.maxmind.com/en/geolite2/signup", flush=True)
            else:
                print("     Could not detect public IP address.", flush=True)
                print("     Configure 'location_ip_address' in config.json", flush=True)
                print("     or run the setup wizard to configure location.", flush=True)
        else:
            print(f"📍 {location_context}", flush=True)
    else:
        print("📍 Location services disabled", flush=True)

    # Initialize TTS
    print(f"🔊 Initializing TTS engine ({cfg.tts_engine})...", flush=True)
    tts = create_tts_engine(
        engine=cfg.tts_engine,
        enabled=cfg.tts_enabled,
        voice=cfg.tts_voice,
        rate=cfg.tts_rate,
        # Chatterbox parameters
        device=cfg.tts_chatterbox_device,
        audio_prompt_path=cfg.tts_chatterbox_audio_prompt,
        exaggeration=cfg.tts_chatterbox_exaggeration,
        cfg_weight=cfg.tts_chatterbox_cfg_weight,
        # Piper parameters
        piper_model_path=cfg.tts_piper_model_path,
        piper_speaker=cfg.tts_piper_speaker,
        piper_length_scale=cfg.tts_piper_length_scale,
        piper_noise_scale=cfg.tts_piper_noise_scale,
        piper_noise_w=cfg.tts_piper_noise_w,
        piper_sentence_silence=cfg.tts_piper_sentence_silence,
        # Voice outputs: this PC and any an extension adds
        pc_speakers_enabled=cfg.tts_pc_speakers_enabled,
        voice_outputs=extensions.voice_outputs(),
    )
    _global_tts_engine = tts  # Expose for face widget speaking animation
    if tts.enabled:
        tts.start()
        print("✓ TTS engine started", flush=True)
    else:
        print("  TTS disabled", flush=True)

    # Initialize voice listening (only if dependencies available)
    print("🎤 Preparing speech recognition in the background...", flush=True)
    voice_thread: Optional[threading.Thread] = None
    voice_thread = VoiceListener(db, cfg, tts, _global_dialogue_memory)
    voice_thread.start()
    _global_voice_listener = voice_thread

    # Initialize dictation engine (hold-to-dictate)
    dictation = None
    if bool(getattr(cfg, "dictation_enabled", True)):
        try:
            from .dictation.dictation_engine import DictationEngine as _DE  # noqa: F811

            def _on_dictation_start():
                voice_thread._dictation_active = True
                set_state(AssistantState.DICTATING)
                debug_log("dictation started — listener paused", "dictation")

            def _on_dictation_processing_start():
                set_state(AssistantState.DICTATION_PROCESSING)
                debug_log("dictation processing started — transcribing captured audio", "dictation")

            def _on_dictation_end():
                voice_thread._dictation_active = False
                set_state(AssistantState.IDLE)
                debug_log("dictation ended — listener resumed", "dictation")

            dictation = _DE(
                whisper_model_ref=lambda: voice_thread.model,
                whisper_backend_ref=lambda: voice_thread._whisper_backend,
                mlx_repo_ref=lambda: voice_thread._mlx_model_repo,
                hotkey=cfg.dictation_hotkey,
                sample_rate=int(getattr(cfg, "sample_rate", 16000)),
                on_dictation_start=_on_dictation_start,
                on_dictation_processing_start=_on_dictation_processing_start,
                on_dictation_end=_on_dictation_end,
                transcribe_lock=voice_thread.transcribe_lock,
                voice_device=getattr(cfg, "voice_device", None),
                filler_removal=getattr(cfg, "dictation_filler_removal", False),
                custom_dictionary=getattr(cfg, "dictation_custom_dictionary", []),
                cfg=cfg,
                thinking=getattr(cfg, "dictation_thinking_enabled", False),
            )
            dictation.start()
            _global_dictation_engine = dictation
            if dictation._started:
                from jarvis.dictation.dictation_engine import format_hotkey_display
                hotkey_display = format_hotkey_display(cfg.dictation_hotkey)
                print(f"🎙️ Dictation enabled (hold {hotkey_display} to dictate)", flush=True)
        except Exception as e:
            debug_log(f"dictation engine init failed: {e}", "dictation")
            print(f"  ⚠ Dictation not available: {e}", flush=True)
    else:
        print("🎙️ Dictation disabled", flush=True)

    if smoke_test:
        if mcp_discovery is not None:
            mcp_discovery.join(timeout=150.0)
        print("SMOKE_TEST_INIT_OK", flush=True)
        debug_log("smoke test: all components initialised successfully", "jarvis")

        # Clean shutdown: stop engines, close database, tear down MCP runtime.
        # The caller is responsible for printing SMOKE_TEST_PASSED / FAILED.
        if dictation is not None:
            try:
                dictation.stop()
            except Exception:
                pass

        _global_voice_listener = None
        if voice_thread is not None:
            try:
                voice_thread.stop()
                voice_thread.join(timeout=2.0)
            except Exception:
                pass

        if tts is not None:
            try:
                tts.stop()
            except Exception:
                pass

        try:
            from .tools.external.mcp_runtime import shutdown_runtime
            shutdown_runtime()
        except Exception:
            pass

        reply_modes.stop()
        activity_runtime.stop()
        extensions.stop()
        db.close()

        if _warm_profile_graph_listener is not None:
            try:
                from .memory.graph import unregister_graph_mutation_listener
                unregister_graph_mutation_listener(_warm_profile_graph_listener)
            except Exception:
                pass
            _warm_profile_graph_listener = None

        # Reset module-level globals so in-process re-entry is clean.
        from .tools.confirmation import ORIGIN_CHAT, set_result_handler
        set_result_handler(None, origin=ORIGIN_CHAT)
        _global_dialogue_memory = None
        _global_tts_engine = None
        _global_dictation_engine = None

        return

    # Opt-in phone access: serves nothing unless the user turned it on in Settings.
    from .remote import runtime as remote_runtime
    remote_runtime.start(cfg)

    # Periodic diary update checking
    last_diary_check = time.time()
    diary_check_interval = 60.0

    # Start stdin monitor thread.
    # Two jobs:
    #   1. Windows shutdown signal: CTRL_BREAK_EVENT doesn't work reliably with
    #      CREATE_NO_WINDOW, so we treat stdin EOF / a bare "SHUTDOWN" line as a
    #      stop request (unchanged behaviour).
    #   2. Subprocess chat query-in: the desktop app writes
    #      ``__CHAT_QUERY__:{"text":"..."}`` lines so the chat window can submit
    #      text when the daemon runs as a separate process. Non-chat lines are
    #      ignored so the monitor is a no-op for users who never open the chat.
    def stdin_monitor():
        try:
            # When parent closes our stdin, readline returns empty
            while True:
                line = sys.stdin.readline()
                if not line:  # EOF - stdin closed
                    debug_log("stdin closed, requesting stop", "jarvis")
                    request_stop()
                    break
                stripped = line.strip()
                if stripped == "SHUTDOWN":
                    debug_log("SHUTDOWN command received, requesting stop", "jarvis")
                    request_stop()
                    break
                # Chat query-in (subprocess mode). Returns False for any other
                # line, which we silently ignore.
                if handle_chat_cancel_stdin_line(stripped):
                    continue
                if handle_wake_stdin_line(stripped):
                    continue
                if handle_reply_mode_stdin_line(stripped):
                    continue
                if handle_activity_stdin_line(stripped):
                    continue
                if handle_chat_new_session_stdin_line(stripped):
                    continue
                if handle_chat_rewind_stdin_line(stripped):
                    continue
                if handle_chat_restore_stdin_line(stripped):
                    continue
                if stripped.startswith(CHAT_QUERY_IPC_PREFIX):
                    handle_chat_query_stdin_line(stripped)
        except Exception:
            pass  # stdin might not be available

    if _stdin_ipc_enabled():
        stdin_thread = threading.Thread(target=stdin_monitor, daemon=True)
        stdin_thread.start()

    try:
        # Main daemon loop
        while not _global_stop_requested:
            time.sleep(1.0)
            now = time.time()

            # Periodically check if diary should be updated
            if now - last_diary_check >= diary_check_interval:
                _check_and_update_diary(db, cfg, verbose=False)
                last_diary_check = now

        # Keep voice thread alive (unless stop requested)
        if voice_thread is not None:
            while voice_thread.is_alive() and not _global_stop_requested:
                time.sleep(0.5)
                _check_and_update_diary(db, cfg, verbose=False)

    except KeyboardInterrupt:
        debug_log("daemon received KeyboardInterrupt", "jarvis")
    finally:
        print("🔄 Daemon shutting down - saving memory...", flush=True)
        debug_log("daemon finally block starting - performing cleanup", "jarvis")

        # No new phone requests once shutdown has begun.
        remote_runtime.stop()

        # Clean shutdown - stop dictation first
        if dictation is not None:
            debug_log("stopping dictation engine...", "jarvis")
            dictation.stop()
            debug_log("dictation engine stopped", "jarvis")

        _global_voice_listener = None
        if voice_thread is not None:
            debug_log("stopping voice thread...", "jarvis")
            voice_thread.stop()
            try:
                voice_thread.join(timeout=2.0)
            except Exception:
                pass
            debug_log("voice thread stopped", "jarvis")

        # A chat worker that started just before the stop request is still
        # inside run_reply_engine, holding the database connection the
        # diary pass below is about to use and the one db.close() will
        # shut. Wait for it, bounded: quitting a moment late beats a
        # closed-handle raise or a diary pass racing its writes.
        wait_for_chat_worker(timeout_sec=5.0)

        # Final diary update before shutdown
        debug_log("performing final diary update (force=True)...", "jarvis")
        print("📝 Updating diary before shutdown...", flush=True)

        # Check dialogue memory status
        if _global_dialogue_memory is None:
            print("⚠️ Dialogue memory is None - nothing to save", flush=True)
        else:
            # Display-only count; actual save uses the atomic snapshot path.
            pending = _global_dialogue_memory.get_pending_chunks()
            print(f"💬 Found {len(pending)} pending conversation chunks", flush=True)

        # Use callbacks if they were set by desktop app (for live UI updates in bundled mode)
        # Use IPC (stdout events) if callbacks not set (subprocess mode)
        use_callbacks = any(_diary_update_callbacks.values())
        use_ipc = not use_callbacks  # Subprocess mode - emit events to stdout
        _check_and_update_diary(db, cfg, verbose=True, force=True, timeout_sec=SHUTDOWN_DIARY_TIMEOUT_SEC, use_callbacks=use_callbacks, use_ipc=use_ipc)
        print("✅ Diary update complete", flush=True)
        debug_log("diary update complete", "jarvis")

        if tts is not None:
            tts.stop()

        # Tear down persistent MCP sessions so subprocess-launched
        # children (e.g. chrome-devtools-mcp's Chrome) close cleanly.
        try:
            from .tools.external.mcp_runtime import shutdown_runtime
            shutdown_runtime()
        except Exception as _e:
            debug_log(f"MCP runtime shutdown error: {_e}", "jarvis")

        reply_modes.stop()
        activity_runtime.stop()
        extensions.stop()
        db.close()

        # Drop the warm-profile graph listener so the module registry does
        # not retain a closure pointing at this run's DialogueMemory after
        # shutdown — relevant for tests and any embedder that re-runs the
        # daemon in-process.
        if _warm_profile_graph_listener is not None:
            try:
                from .memory.graph import unregister_graph_mutation_listener
                unregister_graph_mutation_listener(_warm_profile_graph_listener)
            except Exception:
                pass
            _warm_profile_graph_listener = None

        debug_log("daemon stopped", "jarvis")
        print("👋 Daemon stopped", flush=True)


if __name__ == "__main__":
    import sys as _sys
    smoke_test = "--smoke-test" in set(_sys.argv[1:])
    main(smoke_test=smoke_test)
