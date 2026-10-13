"""Desktop confirmation dialogs for a daemon that runs as a subprocess of the desktop app.

The desktop app owns the daemon's stdin and stdout. This presenter asks it to show the dialog with a
``__CONFIRM__:`` line on stdout and receives the answer as a ``__CONFIRM_ANSWER__:`` line on stdin, so
the confirmation store works exactly as with the in-process dialog. Only a plain ``true`` approves;
anything else, and no answer before the store's expiry, approves nothing. See ``desktop_app.spec.md``
(Action Confirmation Dialog).
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, Optional

from ..debug import debug_log

# daemon -> desktop: {"type": "open", "data": {"id", "action", "target", "consequence"}}
#                    {"type": "close", "data": {"id"}}
CONFIRM_IPC_PREFIX = "__CONFIRM__:"
# desktop -> daemon: {"id": "<request id>", "approved": true | false}
CONFIRM_ANSWER_IPC_PREFIX = "__CONFIRM_ANSWER__:"


class _PipeDialogHandle:
    """The store's handle for a dialog shown by the desktop app; ``close()`` is safe from any thread."""

    def __init__(self, presenter: "PipeDialogPresenter", request_id: str) -> None:
        self._presenter = presenter
        self._request_id = request_id

    def close(self) -> None:
        self._presenter._close(self._request_id)


class PipeDialogPresenter:
    """A ``DialogPresenter`` that shows the dialog in the desktop app over the daemon's pipes.

    ``write_line(line)`` sends one line to the desktop app (the daemon's stdout).
    """

    def __init__(self, write_line: Callable[[str], None]) -> None:
        self._write_line = write_line
        self._lock = threading.Lock()
        self._waiting: Dict[str, Callable[[bool], Any]] = {}

    def __call__(self, request: Any, resolve: Callable[[bool], Any]) -> Optional[_PipeDialogHandle]:
        request_id = str(request.id)
        with self._lock:
            self._waiting[request_id] = resolve
        data = {
            "id": request_id,
            "action": str(request.action or ""),
            "target": str(request.target or ""),
            "consequence": str(request.consequence) if request.consequence else None,
        }
        if not self._send("open", data):
            with self._lock:
                self._waiting.pop(request_id, None)
            return None
        # Tool name only: the action and target can name a file, a process or an app.
        debug_log(f"Asked the desktop app to confirm {request.tool_name}", "safety")
        return _PipeDialogHandle(self, request_id)

    def handle_answer_line(self, line: str) -> bool:
        """Apply a ``__CONFIRM_ANSWER__:`` line. True when the line was an answer, even an unusable one."""
        if not line.startswith(CONFIRM_ANSWER_IPC_PREFIX):
            return False
        try:
            payload = json.loads(line[len(CONFIRM_ANSWER_IPC_PREFIX):])
        except ValueError:
            payload = None
        request_id = payload.get("id") if isinstance(payload, dict) else None
        approved = payload.get("approved") if isinstance(payload, dict) else None
        if not isinstance(request_id, str) or not isinstance(approved, bool):
            debug_log("Ignored a malformed desktop confirmation answer", "safety")
            return True
        with self._lock:
            resolve = self._waiting.pop(request_id, None)
        if resolve is None:
            debug_log("Ignored a desktop confirmation answer for no open dialog", "safety")
            return True
        debug_log(f"Desktop confirmation answered: {'confirmed' if approved else 'cancelled'}", "safety")
        try:
            resolve(approved)
        except Exception as exc:
            # Runs on the stdin monitor, which must keep reading whatever happens here.
            debug_log(f"Applying the desktop confirmation answer failed: {type(exc).__name__}", "safety")
        return True

    def _close(self, request_id: str) -> None:
        with self._lock:
            waiting = self._waiting.pop(request_id, None) is not None
        if waiting:
            self._send("close", {"id": request_id})

    def _send(self, event_type: str, data: dict) -> bool:
        try:
            self._write_line(CONFIRM_IPC_PREFIX + json.dumps({"type": event_type, "data": data}))
            return True
        except Exception as exc:
            debug_log(f"Desktop confirmation event not sent: {type(exc).__name__}", "safety")
            return False


__all__ = ["CONFIRM_IPC_PREFIX", "CONFIRM_ANSWER_IPC_PREFIX", "PipeDialogPresenter"]
