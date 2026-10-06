"""The tray items for the opt-in activity log: a pause toggle and a confirmed "delete history".

Choosing an item only asks for the change: the daemon (bundled thread or subprocess over stdin) or,
with no daemon running, the stored settings and database apply it. See
``jarvis/memory/activity_log.spec.md``.
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Tuple

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QMessageBox

from desktop_app.themes import line_icon
from jarvis.debug import debug_log

_DELETE_QUESTION = ("Delete all recorded activity history?\n\nThis removes every recorded application and "
                    "window title from this PC. It cannot be undone.")


def request_line(action: str) -> str:
    """The stdin line asking a subprocess daemon to ``pause``, ``resume``, ``delete`` or ``stop_sharing``."""
    from jarvis.daemon import ACTIVITY_IPC_PREFIX
    return ACTIVITY_IPC_PREFIX + json.dumps({"action": action})


def configured_state() -> Tuple[bool, bool]:
    """``(enabled, paused)`` from Settings, for the menu and for when no daemon is running."""
    from jarvis.config import load_settings
    cfg = load_settings()
    return cfg.activity_log_enabled, cfg.activity_log_paused


def confirm_delete(text: str = _DELETE_QUESTION) -> bool:
    answer = QMessageBox.question(None, "Delete activity history", text,
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def send_request(app, action: str) -> None:
    """Apply ``pause``, ``resume``, ``delete`` or ``stop_sharing`` through whichever daemon the app has, or
    directly. With no daemon, ``stop_sharing`` has nothing to do: the saved setting is read at start."""
    debug_log(f"tray requested activity log {action}", "desktop")
    try:
        if getattr(app, "is_listening", False) and getattr(app, "is_bundled", False):
            from jarvis import daemon
            call = {"pause": lambda: daemon.set_activity_paused(True),
                    "resume": lambda: daemon.set_activity_paused(False),
                    "delete": daemon.delete_activity_history,
                    "stop_sharing": daemon.stop_activity_cloud_sharing}[action]
            # Deleting touches the database; keep it off the GUI thread.
            threading.Thread(target=call, daemon=True, name="tray-activity").start()
        elif getattr(app, "is_listening", False) and getattr(app, "daemon_process", None) is not None:
            app.daemon_process.stdin.write(request_line(action) + "\n")
            app.daemon_process.stdin.flush()
        elif action == "stop_sharing":
            return
        elif action == "delete":
            from jarvis.memory import activity_runtime
            activity_runtime.delete_history()
        else:
            from jarvis.config import update_config_values
            update_config_values({"activity_log_paused": action == "pause"})
    except Exception as exc:
        debug_log(f"tray activity request failed: {type(exc).__name__}", "desktop")


class ActivityMenu(QObject):
    """``Pause Activity Log`` toggle and ``Delete Activity History…`` items."""

    pause_toggled = pyqtSignal(bool)
    delete_confirmed = pyqtSignal()

    def __init__(self, parent: QObject | None = None, *, confirm: Callable[[str], bool] = confirm_delete):
        super().__init__(parent)
        self._confirm = confirm
        self.pause_action = QAction("Pause Activity Log")
        self.pause_action.setIcon(line_icon("pause"))
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self.pause_toggled.emit)
        self.delete_action = QAction("Delete Activity History…")
        self.delete_action.setIcon(line_icon("trash"))
        self.delete_action.triggered.connect(self._delete_chosen)
        self.set_state(enabled=False, paused=False)

    def add_to(self, menu: QMenu) -> None:
        menu.addAction(self.pause_action)
        menu.addAction(self.delete_action)

    def set_state(self, *, enabled: bool, paused: bool) -> None:
        """Show the stored state without emitting a request."""
        self.pause_action.blockSignals(True)
        self.pause_action.setChecked(bool(paused and enabled))
        self.pause_action.blockSignals(False)
        self.pause_action.setEnabled(enabled)
        self.pause_action.setText("Pause Activity Log" if enabled else "Pause Activity Log (turn on in Settings)")

    def refresh_from_settings(self) -> None:
        try:
            enabled, paused = configured_state()
        except Exception as exc:
            debug_log(f"activity menu state unavailable: {type(exc).__name__}", "desktop")
            return
        self.set_state(enabled=enabled, paused=paused)

    def _delete_chosen(self) -> None:
        if self._confirm(_DELETE_QUESTION):
            self.delete_confirmed.emit()
