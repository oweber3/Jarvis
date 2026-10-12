"""The page's microphone button, server side: one recording at a time, kept alive by the page.

The daemon's dictation engine records and transcribes (``DictationEngine.start_capture``), never the
browser. A recording belongs to the page that started it, which holds an unguessable session id and must
keep calling ``beat``; a page that closes, crashes or loses the connection stops beating and its
recording is cancelled, so the microphone is never left open. The transcript is returned by ``stop`` to
the caller and is never stored or logged. See ``webchat.spec.md``, Voice input.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from ..debug import debug_log

MAX_RECORDING_SEC = 120.0
HEARTBEAT_TIMEOUT_SEC = 6.0
WATCH_INTERVAL_SEC = 1.0


class MicSessions:
    """The page's one microphone recording. ``backend`` is a ``DaemonBackend`` or a test fake."""

    def __init__(self, backend, *, heartbeat_timeout_sec: float = HEARTBEAT_TIMEOUT_SEC,
                 watch_interval_sec: float = WATCH_INTERVAL_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._backend = backend
        self._timeout = heartbeat_timeout_sec
        self._interval = watch_interval_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._open: Optional[Dict[str, Any]] = None  # {"session", "handle", "last_beat", "started"}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- the page's calls ----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Whether a recording can start now, and if not why (``busy``, ``unavailable``, ``disabled``,
        ``not_ready`` or ``microphone``)."""
        with self._lock:
            reason = "busy" if self._open is not None else None
        if reason is None:
            reason = self._backend.mic_reason()
        return {"available": reason is None, "reason": reason, "max_seconds": MAX_RECORDING_SEC}

    def start(self) -> Tuple[Optional[str], Optional[str]]:
        """Begin recording: ``(session, None)``, or ``(None, reason)`` when it cannot."""
        with self._lock:
            if self._open is not None:
                return None, "busy"
            handle, reason = self._backend.mic_start(MAX_RECORDING_SEC)
            if handle is None:
                debug_log(f"webchat: microphone refused ({reason})", "webchat")
                return None, reason or "unavailable"
            now = self._clock()
            session = secrets.token_hex(16)
            self._open = {"session": session, "handle": handle, "last_beat": now, "started": now}
            self._ensure_watch_locked()
        debug_log("webchat: microphone recording started", "webchat")
        return session, None

    def beat(self, session: str) -> Optional[str]:
        """Keep the recording alive: ``recording``, ``full`` (the length cap was reached and the page
        should stop it), or ``None`` when this page has no recording any more."""
        with self._lock:
            record = self._owned_locked(session)
            if record is None:
                return None
            state = self._backend.mic_state(record["handle"])
            if state is None:
                self._open = None
                debug_log("webchat: microphone recording was lost", "webchat")
                return None
            record["last_beat"] = self._clock()
            return state

    def stop(self, session: str) -> Optional[str]:
        """End the recording and return its transcript (possibly empty); ``None`` for an unknown session.

        Blocks while the audio is transcribed. The text goes to the caller and nowhere else."""
        record = self._take(session)
        if record is None:
            return None
        text = self._backend.mic_stop(record["handle"])
        seconds = self._clock() - record["started"]
        debug_log(f"webchat: microphone recording stopped after {seconds:.0f}s", "webchat")
        return text

    def cancel(self, session: str) -> bool:
        """Drop the recording and release the microphone. False for an unknown session."""
        record = self._take(session)
        if record is None:
            return False
        self._backend.mic_cancel(record["handle"])
        debug_log("webchat: microphone recording cancelled", "webchat")
        return True

    def shutdown(self) -> None:
        """Release the microphone and end the watch (the web chat stops)."""
        self._stop.set()
        with self._lock:
            record, self._open = self._open, None
        if record is not None:
            self._backend.mic_cancel(record["handle"])
            debug_log("webchat: microphone released at shutdown", "webchat")
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # -- internals -----------------------------------------------------------------

    def _owned_locked(self, session: Any) -> Optional[Dict[str, Any]]:
        record = self._open
        if record is None or not isinstance(session, str):
            return None
        return record if hmac.compare_digest(record["session"], session) else None

    def _take(self, session: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._owned_locked(session)
            if record is not None:
                self._open = None
            return record

    def _ensure_watch_locked(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="jarvis-webchat-mic", daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        """Cancel a recording whose page stopped beating. Ends when there is no recording to watch."""
        while not self._stop.wait(self._interval):
            with self._lock:
                record = self._open
                if record is None:
                    self._thread = None
                    return
                lost = self._clock() - record["last_beat"] > self._timeout
                if lost:
                    self._open = None
            if lost:
                self._backend.mic_cancel(record["handle"])
                debug_log("webchat: microphone released, the page stopped answering", "webchat")
