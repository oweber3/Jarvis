"""The tray items for opt-in document search: "rebuild the index" and a confirmed "delete the index".

Choosing an item only asks for the change: the daemon (bundled thread or subprocess over stdin) or, with
no daemon running, the database applies it. See ``jarvis/documents/documents.spec.md``.
"""
from __future__ import annotations

import json
import threading
from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QMessageBox

from desktop_app.themes import line_icon
from jarvis.debug import debug_log

_DELETE_QUESTION = ("Delete the document search index?\n\nThis removes every stored passage of your documents "
                    "from Jarvis's database on this PC. Your documents themselves are not touched. It cannot be "
                    "undone; with document search on, the index is built again at the next check.")


def request_line(action: str) -> str:
    """The stdin line asking a subprocess daemon to ``rebuild``, ``delete`` or ``stop_sharing``."""
    from jarvis.daemon import DOCUMENTS_IPC_PREFIX
    return DOCUMENTS_IPC_PREFIX + json.dumps({"action": action})


def configured_enabled() -> bool:
    from jarvis.config import load_settings
    return load_settings().document_search_enabled


def confirm_delete(text: str = _DELETE_QUESTION) -> bool:
    answer = QMessageBox.question(None, "Delete document index", text,
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def send_request(app, action: str) -> None:
    """Apply ``rebuild``, ``delete`` or ``stop_sharing`` through whichever daemon the app has, or directly.
    With no daemon, rebuild and delete both delete the index (it is built again at the next start) and
    ``stop_sharing`` has nothing to do."""
    debug_log(f"tray requested document index {action}", "desktop")
    try:
        if getattr(app, "is_listening", False) and getattr(app, "is_bundled", False):
            from jarvis import daemon
            call = {"rebuild": daemon.rebuild_document_index,
                    "delete": daemon.delete_document_index,
                    "stop_sharing": daemon.stop_document_cloud_sharing}[action]
            # Deleting touches the database; keep it off the GUI thread.
            threading.Thread(target=call, daemon=True, name="tray-documents").start()
        elif getattr(app, "is_listening", False) and getattr(app, "daemon_process", None) is not None:
            app.daemon_process.stdin.write(request_line(action) + "\n")
            app.daemon_process.stdin.flush()
        elif action in ("rebuild", "delete"):
            from jarvis.documents import runtime
            runtime.delete_index()
    except Exception as exc:
        debug_log(f"tray document request failed: {type(exc).__name__}", "desktop")


class DocumentsMenu(QObject):
    """``Rebuild Document Index`` and ``Delete Document Index…`` items."""

    rebuild_requested = pyqtSignal()
    delete_confirmed = pyqtSignal()

    def __init__(self, parent: QObject | None = None, *, confirm: Callable[[str], bool] = confirm_delete):
        super().__init__(parent)
        self._confirm = confirm
        self.rebuild_action = QAction("Rebuild Document Index")
        self.rebuild_action.setIcon(line_icon("refresh"))
        self.rebuild_action.triggered.connect(self.rebuild_requested.emit)
        self.delete_action = QAction("Delete Document Index…")
        self.delete_action.setIcon(line_icon("trash"))
        self.delete_action.triggered.connect(self._delete_chosen)
        self.set_state(enabled=False)

    def add_to(self, menu: QMenu) -> None:
        menu.addAction(self.rebuild_action)
        menu.addAction(self.delete_action)

    def set_state(self, *, enabled: bool) -> None:
        self.rebuild_action.setEnabled(enabled)
        self.rebuild_action.setText("Rebuild Document Index" if enabled
                                    else "Rebuild Document Index (turn on in Settings)")

    def refresh_from_settings(self) -> None:
        try:
            enabled = configured_enabled()
        except Exception as exc:
            debug_log(f"documents menu state unavailable: {type(exc).__name__}", "desktop")
            return
        self.set_state(enabled=enabled)

    def _delete_chosen(self) -> None:
        if self._confirm(_DELETE_QUESTION):
            self.delete_confirmed.emit()
