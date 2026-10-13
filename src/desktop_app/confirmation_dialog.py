"""Desktop action confirmation dialog for high-risk actions."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional
from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from jarvis.debug import debug_log

from .themes import apply_theme, divider, hud_heading


class ActionConfirmationDialog(QDialog):
    """Modal dialog for confirming high-risk assistant actions."""

    def __init__(
        self,
        action: str,
        target: str,
        consequence: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.action = action
        self.target = target
        self.consequence = consequence
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("Action Confirmation")
        self.setMinimumWidth(440)
        self.setModal(True)
        apply_theme(self)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 20, 24, 20)

        # Title
        heading, _, _ = hud_heading("Confirm", "Confirmation required",
                                    "Jarvis is waiting for your answer before it acts.")
        layout.addWidget(heading)
        layout.addWidget(divider())

        # Details frame
        frame = QFrame()
        frame.setObjectName("inset")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(14, 12, 14, 12)
        frame_layout.setSpacing(8)

        # Dynamic values are plain text: they can carry process names, paths or
        # model-supplied text that must never be interpreted as markup or links.
        rows = [("Action", self.action), ("Target", self.target), ("Consequence", self.consequence)]
        for caption, value in rows:
            if not value:
                continue
            row = QHBoxLayout()
            row.setSpacing(6)
            caption_label = QLabel(caption)
            caption_label.setTextFormat(Qt.TextFormat.PlainText)
            caption_label.setObjectName("key")
            caption_label.setMinimumWidth(96)
            caption_label.setAlignment(Qt.AlignmentFlag.AlignTop)
            value_label = QLabel(str(value))
            value_label.setTextFormat(Qt.TextFormat.PlainText)
            value_label.setOpenExternalLinks(False)
            value_label.setWordWrap(True)
            row.addWidget(caption_label)
            row.addWidget(value_label, 1)
            frame_layout.addLayout(row)

        layout.addWidget(frame)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)
        button_layout.addStretch(1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)

        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.confirm_button.setObjectName("danger")
        self.confirm_button.clicked.connect(self.accept)

        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.confirm_button)
        layout.addLayout(button_layout)


class _DialogBridge(QObject):
    """Lives on the GUI thread; carries open/close requests from any thread."""

    open_request = pyqtSignal(object)
    close_request = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.open_request.connect(self._on_open)
        self.close_request.connect(self._on_close)

    def _on_open(self, handle) -> None:
        handle._open_on_gui_thread()

    def _on_close(self, handle) -> None:
        handle._close_on_gui_thread()


_BRIDGE: Optional[_DialogBridge] = None
_BRIDGE_LOCK = threading.Lock()
# Dialogs stay referenced until answered or closed so Qt never collects an open window.
_ACTIVE: set = set()


def _get_bridge() -> Optional[_DialogBridge]:
    global _BRIDGE
    with _BRIDGE_LOCK:
        app = QApplication.instance()
        if _BRIDGE is None and app is not None:
            bridge = _DialogBridge()
            # Receivers must live on the GUI thread so slots never run on a worker.
            bridge.moveToThread(app.thread())
            _BRIDGE = bridge
        return _BRIDGE


class DialogHandle:
    """Thread-safe handle for one confirmation dialog.

    ``close()`` may be called from any thread (for example on expiry); the
    dialog is dismissed on the GUI thread without reporting an answer. A
    user answer reaches ``resolve`` exactly once.
    """

    def __init__(self, request: Any, resolve: Callable[[bool], Any], bridge: _DialogBridge):
        self._request = request
        self._resolve = resolve
        self._bridge = bridge
        self._lock = threading.Lock()
        self._closed = False
        self._answered = False
        self.dialog: Optional[ActionConfirmationDialog] = None

    # -- any thread ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._bridge.close_request.emit(self)
        except RuntimeError:
            pass  # bridge destroyed with the application

    # -- GUI thread only -----------------------------------------------------

    def _open_on_gui_thread(self) -> None:
        with self._lock:
            if self._closed:
                return
        dlg = ActionConfirmationDialog(
            action=self._request.action,
            target=self._request.target,
            consequence=self._request.consequence,
        )
        dlg.finished.connect(self._on_finished)
        self.dialog = dlg
        _ACTIVE.add(self)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _close_on_gui_thread(self) -> None:
        dlg = self.dialog
        if dlg is not None and dlg.isVisible():
            dlg.close()
        _ACTIVE.discard(self)

    def _on_finished(self, result: int) -> None:
        with self._lock:
            if self._answered or self._closed:
                _ACTIVE.discard(self)
                return
            self._answered = True
            self._closed = True
        _ACTIVE.discard(self)
        # Cancel, Escape and the window close button all arrive as a rejection.
        self._resolve(result == QDialog.DialogCode.Accepted.value)


def show_desktop_confirmation_dialog(request: Any, resolve: Callable[[bool], Any]) -> Optional[DialogHandle]:
    """Open the confirmation dialog without blocking and return its handle.

    Safe from any thread: the dialog is created and shown on the GUI thread.
    Returns ``None`` when no Qt application is running.
    """
    app = QApplication.instance()
    if app is None:
        return None
    bridge = _get_bridge()
    if bridge is None:
        return None
    handle = DialogHandle(request, resolve, bridge)
    if QThread.currentThread() == app.thread():
        handle._open_on_gui_thread()
    else:
        bridge.open_request.emit(handle)
    return handle


@dataclass(frozen=True)
class PipedConfirmationRequest:
    """What the dialog shows for a request the daemon subprocess sent."""
    id: str
    action: str
    target: str
    consequence: Optional[str] = None


def _optional_text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("not text")


class SubprocessConfirmationRelay:
    """Shows the dialogs a daemon subprocess asks for and writes each answer to its stdin.

    ``write_line(line)`` sends one line to the daemon. ``handle_line`` takes the daemon's
    ``__CONFIRM__:`` events on the GUI thread. A dialog closed by the daemon, by ``close_all``
    (the daemon went away) or by its own timeout (the daemon's expiry plus a margin, in case the
    daemon's close never arrives) answers nothing, which the daemon treats as a refusal. See
    ``desktop_app.spec.md`` (Action Confirmation Dialog).
    """

    def __init__(self, write_line: Callable[[str], Any],
                 presenter: Optional[Callable[[Any, Callable[[bool], Any]], Any]] = None,
                 timeout_sec: Optional[float] = None) -> None:
        from jarvis.tools.confirmation import DIALOG_TIMEOUT_SEC
        self._write_line = write_line
        self._presenter = presenter or show_desktop_confirmation_dialog
        self._timeout_sec = timeout_sec if timeout_sec is not None else DIALOG_TIMEOUT_SEC + 5.0
        self._lock = threading.Lock()
        # request id -> [dialog handle (None while it opens), expiry timer]
        self._open: dict = {}
        self._closed = False

    def handle_line(self, line: str) -> bool:
        """Apply a ``__CONFIRM__:`` event. True when the line was one, even a malformed one."""
        from jarvis.tools.confirmation_pipe import CONFIRM_IPC_PREFIX
        if not line.startswith(CONFIRM_IPC_PREFIX):
            return False
        try:
            event = json.loads(line[len(CONFIRM_IPC_PREFIX):])
            kind, data = event.get("type"), event.get("data")
            request_id = data.get("id")
            if not isinstance(request_id, str):
                raise ValueError("no id")
            if kind == "open":
                request = PipedConfirmationRequest(
                    id=request_id,
                    action=_optional_text(data.get("action")) or "",
                    target=_optional_text(data.get("target")) or "",
                    consequence=_optional_text(data.get("consequence")),
                )
        except (ValueError, AttributeError):
            debug_log("desktop confirmation: malformed event ignored", "desktop")
            return True
        if kind == "open":
            self._open_dialog(request)
        elif kind == "close":
            self._close(request_id)
        else:
            debug_log("desktop confirmation: unknown event ignored", "desktop")
        return True

    def close_all(self) -> None:
        """Dismiss every dialog without answering and show no more (the daemon stopped or crashed)."""
        with self._lock:
            self._closed = True
            entries, self._open = list(self._open.values()), {}
        for handle, timer in entries:
            timer.cancel()
            self._dismiss(handle)
        if entries:
            debug_log(f"desktop confirmation: closed {len(entries)} dialog(s), the daemon went away", "desktop")

    def _open_dialog(self, request: PipedConfirmationRequest) -> None:
        def resolve(approved: bool) -> None:
            if self._take(request.id) is None:
                return  # closed, timed out or already answered
            self._answer(request.id, bool(approved))

        timer = threading.Timer(self._timeout_sec, self._expire, args=(request.id,))
        timer.daemon = True
        with self._lock:
            if self._closed or request.id in self._open:
                debug_log("desktop confirmation: request ignored (daemon gone or already shown)", "desktop")
                return
            self._open[request.id] = [None, timer]
        timer.start()
        handle = self._presenter(request, resolve)
        if handle is None:
            if self._take(request.id) is not None:
                debug_log("desktop confirmation: dialog could not be shown, declining", "desktop")
                self._answer(request.id, False)
            return
        with self._lock:
            entry = self._open.get(request.id)
            if entry is not None:
                entry[0] = handle
                debug_log("desktop confirmation: dialog shown for the daemon", "desktop")
                return
        self._dismiss(handle)  # answered, closed or timed out while it was being opened

    def _take(self, request_id: str) -> Optional[list]:
        """Remove a request's entry (stopping its timer); ``None`` if it was no longer open."""
        with self._lock:
            entry = self._open.pop(request_id, None)
        if entry is not None:
            entry[1].cancel()
        return entry

    def _expire(self, request_id: str) -> None:
        entry = self._take(request_id)
        if entry is not None:
            debug_log("desktop confirmation: dialog timed out unanswered", "desktop")
            self._dismiss(entry[0])

    def _close(self, request_id: str) -> None:
        entry = self._take(request_id)
        if entry is not None:
            self._dismiss(entry[0])

    @staticmethod
    def _dismiss(handle: Any) -> None:
        if handle is None:
            return
        try:
            handle.close()
        except Exception as exc:
            debug_log(f"desktop confirmation: closing the dialog failed: {type(exc).__name__}", "desktop")

    def _answer(self, request_id: str, approved: bool) -> None:
        from jarvis.tools.confirmation_pipe import CONFIRM_ANSWER_IPC_PREFIX
        try:
            self._write_line(CONFIRM_ANSWER_IPC_PREFIX + json.dumps({"id": request_id, "approved": approved}))
        except Exception as exc:
            # The daemon is gone; with no answer nothing runs.
            debug_log(f"desktop confirmation: answer not delivered: {type(exc).__name__}", "desktop")
