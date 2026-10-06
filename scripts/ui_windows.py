"""Offscreen factories for every Jarvis desktop window.

Shared by the contact sheet (``render_ui_contact_sheet.py``), the README
screenshot capture and the per-window theme tests, so each window is built the
same way everywhere, from demo data only: no daemon, no network, no worker
threads, no real configuration, no stored credentials and no real orb state
file. Nothing here ever shows a window on the screen; widgets are rendered with
``grab()`` under Qt's offscreen platform.

Use ``isolated()`` around any use outside the test suite (``tests/conftest.py``
already isolates the same things for tests).
"""

from __future__ import annotations

import os
import socket
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Dict, Iterator, Optional
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


_FONT_FILES = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeui.ttf",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguiemj.ttf",
    ROOT / "src" / "desktop_app" / "desktop_assets" / "fonts" / "DejaVuSans.ttf",
)
_fonts_loaded = False


def load_fonts() -> None:
    """Register UI fonts from files: Qt's offscreen platform finds no system fonts and draws boxes."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    from PyQt6.QtGui import QFontDatabase
    for path in _FONT_FILES:
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    _fonts_loaded = True


class _DemoClock:
    @staticmethod
    def now():
        return datetime(2026, 1, 1, 9, 41)


class _MemoryCredentialStore:
    """Stands in for Windows Credential Manager so no stored key is ever read."""

    def __init__(self):
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@contextmanager
def isolated() -> Iterator[Path]:
    """Isolate configuration, credentials, the orb state file, the network and worker threads."""
    with TemporaryDirectory(prefix="jarvis-ui-") as temporary, ExitStack() as stack:
        root = Path(temporary)
        stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": temporary,
            "XDG_DATA_HOME": temporary,
            "JARVIS_CONFIG_PATH": str(root / "config.json"),
        }))
        stack.enter_context(patch.object(
            socket.socket, "connect", side_effect=AssertionError("UI rendering must be offline")))
        from desktop_app import face_widget
        from desktop_app.qt_worker import KeepAliveWorker
        import jarvis.credentials as credentials
        stack.enter_context(patch.object(
            KeepAliveWorker, "start", side_effect=AssertionError("UI rendering must not start workers")))
        stack.enter_context(patch.object(credentials, "_backend", _MemoryCredentialStore()))
        stack.enter_context(patch.object(face_widget, "_get_jarvis_state_file", lambda: str(root / "jarvis_state")))
        stack.enter_context(patch.object(face_widget, "_jarvis_state_instance", None))
        yield root


def _show_chat():
    from desktop_app import chat_window
    with patch.object(chat_window, "get_hot_window_messages", return_value=[]), \
            patch.object(chat_window, "datetime", _DemoClock):
        window = chat_window.ChatWindow(submit_fn=lambda text: None)
        window.resize(480, 720)
        window.set_reply_mode("claude")
        window._append_user("Help me make room for a slower morning.")
        window._append_assistant(
            "Start with one small thing.\n\nA coffee without a screen, a short walk, "
            "or ten minutes with that book you keep meaning to read.\n\n"
            "What does your morning look like?")
        window._append_user("Coffee and a walk sounds perfect.")
    return window


def _face():
    from desktop_app.face_widget import FaceWindow
    from desktop_app.orb_widget import OrbState
    window = FaceWindow()
    window.resize(380, 440)
    window.orb.set_state_override(OrbState.LISTENING)
    window.set_reply_mode("claude")
    for _ in range(40):
        window.orb.tick(1 / 30)
    return window


def _settings():
    from desktop_app.settings_window import SettingsWindow
    window = SettingsWindow()
    window.resize(960, 680)
    for index in range(window._sidebar.count()):
        if "Speech Recognition" in window._sidebar.item(index).text():
            window._sidebar.setCurrentRow(index)
            break
    return window


def _mcp_catalogue():
    from desktop_app.settings_window import _MCPCatalogueDialog
    return _MCPCatalogueDialog({})


def _mcp_edit():
    from desktop_app.settings_window import _MCPEditDialog
    return _MCPEditDialog("filesystem", {"command": "npx", "args": ["-y", "demo-server"]})


def _wizard_page(page_attribute: str) -> Callable[[], object]:
    def build():
        from desktop_app import setup_wizard
        with patch.object(setup_wizard.ScrollableWizardPage, "initializePage", lambda self: None):
            wizard = setup_wizard.SetupWizard()
            wizard.setStartId(getattr(wizard, page_attribute))
            wizard.resize(960, 680)
            wizard.restart()
        return wizard
    return build


def _splash():
    from desktop_app.splash_screen import SplashScreen
    splash = SplashScreen()
    splash.set_status("Starting daemon...")
    for _ in range(40):
        splash._orb.tick(1 / 30)
    return splash


def _release():
    from desktop_app.updater import ReleaseInfo
    return ReleaseInfo(
        asset_id=1, tag_name="v1.4.0", version="1.4.0", name="Jarvis 1.4.0", prerelease=False,
        html_url="https://example.invalid/release", download_url="https://example.invalid/jarvis.zip",
        asset_name="Jarvis-Windows-x64.zip", asset_size=210 * 1024 * 1024,
        release_notes="## What's new\n- feat: orb HUD theme for every window\n- fix: tray icon legibility\n")


def _update_available():
    from desktop_app.update_dialog import UpdateAvailableDialog
    from desktop_app.updater import UpdateStatus
    release = _release()
    return UpdateAvailableDialog(UpdateStatus(
        update_available=True, current_version="1.3.0", current_channel="stable",
        latest_release=release, releases_since_current=[release]))


def _update_progress():
    from desktop_app.update_dialog import UpdateProgressDialog
    return UpdateProgressDialog(_release())


def _dictation_history():
    import tempfile
    from desktop_app.dictation_history import DictationHistoryWindow
    from jarvis.dictation.history import DictationHistory
    history = DictationHistory(path=Path(tempfile.mkdtemp(prefix="jarvis-ui-")) / "history.json")
    history.add("Remind me to send the invoice before lunch.", 3.2)
    history.add("Move the stand-up to ten past nine.", 2.1)
    window = DictationHistoryWindow(history=history)
    window.resize(700, 560)
    return window


def _confirmation():
    from desktop_app.confirmation_dialog import ActionConfirmationDialog
    return ActionConfirmationDialog(
        action="Close window", target="Untitled - Notepad", consequence="Unsaved text will be lost.")


def _diary():
    from desktop_app.diary_dialog import DiaryUpdateDialog
    dialog = DiaryUpdateDialog()
    dialog.status_label.setText("Writing today's diary entry...")
    return dialog


def _log_viewer():
    from desktop_app import app as desktop_app
    window = desktop_app.LogViewerWindow()
    window.resize(900, 560)
    for line in (
        "🚀 Jarvis daemon started",
        "💾 Initialising dialogue memory…",
        "✓ Dialogue memory initialised",
        "⚠️ 📍 Optional location features unavailable. Add a GeoLite2 database in Setup → Location.",
        "🔊 Initialising TTS engine (piper)…",
        "✓ TTS engine started",
        "🎤 Preparing speech recognition…",
        "weights.npz:  48%|████▊     | 730M/1.52G [00:48<00:52,15.2MB/s]",
    ):
        window.append_log(line)
    return window


def _runtime_status():
    from desktop_app import app as desktop_app
    snapshot = desktop_app.RuntimeStatusSnapshot(
        daemon_state="Listening", daemon_mode="bundled", daemon_pid=None, ollama_needed=True,
        ollama_running=True, ollama_version="0.12.0", ollama_owner="user", ollama_launch_method="",
        low_power_mode=False, llm_provider="ollama", chat_model="gemma4:e4b",
        embedding_provider="ollama", embedding_model="nomic-embed-text", mcp_count=2)
    return desktop_app.RuntimeStatusDialog(snapshot)


def _captured_dialog(open_dialog: Callable[[], object]) -> Callable[[], object]:
    """Build a dialog that its module creates and ``exec()``s internally, without blocking."""
    def build():
        from PyQt6.QtWidgets import QDialog
        captured = []

        def fake_exec(self):
            captured.append(self)
            return QDialog.DialogCode.Rejected

        with patch.object(QDialog, "exec", fake_exec):
            open_dialog()
        return captured[0]
    return build


def _crash_report():
    from desktop_app import app as desktop_app
    desktop_app.show_crash_report_dialog(
        "Fatal Python error: Aborted\n\nThread 0x0000 (most recent call first):\n"
        "  File \"jarvis/daemon.py\", line 120 in run\n")


def _unsupported_model():
    from desktop_app import app as desktop_app
    desktop_app.show_unsupported_model_dialog("some-model:7b")


def _message_box(open_box: Callable[[], object]) -> Callable[[], object]:
    def build():
        from PyQt6.QtWidgets import QMessageBox
        captured = []

        def fake_exec(self):
            captured.append(self)
            return 0

        with patch.object(QMessageBox, "exec", fake_exec):
            open_box()
        return captured[0]
    return build


def _instance_conflict():
    from desktop_app import app as desktop_app
    with patch.object(desktop_app, "get_existing_instance_pid", return_value=4242):
        desktop_app.show_instance_conflict_dialog()


def _no_update():
    from desktop_app.update_dialog import show_no_update_dialog
    show_no_update_dialog("1.3.0")


def _update_error():
    from desktop_app.update_dialog import show_update_error_dialog
    show_update_error_dialog("Could not reach the release server.")


def _reply_mode_menu():
    from desktop_app.reply_mode_menu import ReplyModeMenu
    menu = ReplyModeMenu()
    menu.set_state("claude", ["local", "claude"])
    return menu.menu


def _tray_menu():
    """The real tray menu, built without a tray icon or a daemon."""
    from types import SimpleNamespace
    from desktop_app import app as desktop_app
    tray = desktop_app.JarvisSystemTray.__new__(desktop_app.JarvisSystemTray)
    tray.tray_icon = SimpleNamespace(setContextMenu=lambda menu: None)
    tray.is_listening = False
    tray.is_bundled = False
    tray.create_menu()
    tray.activity_menu.refresh_from_settings()
    tray.menu._tray = tray
    return tray.menu


def _phone_access():
    """Phone access with a demo address and one demo phone in a temporary device store."""
    import tempfile
    from types import SimpleNamespace
    from desktop_app import phone_access_dialog
    from jarvis.remote.pairing import DeviceStore
    directory = Path(tempfile.mkdtemp(prefix="jarvis-ui-"))
    store = DeviceStore(directory)
    store.complete_pairing(store.start_pairing(), "Demo phone")
    cfg = SimpleNamespace(remote_access_enabled=True, remote_access_port=8765, db_path=str(directory / "jarvis.db"))
    with patch.object(phone_access_dialog, "phone_links", lambda port: [f"http://192.168.1.20:{port}/"]):
        return phone_access_dialog.PhoneAccessDialog(cfg)


# name -> factory returning a widget that can be rendered with ``grab()``.
WINDOWS: Dict[str, Callable[[], object]] = {
    "face": _face,
    "chat": _show_chat,
    "settings": _settings,
    "settings-mcp-catalogue": _mcp_catalogue,
    "settings-mcp-edit": _mcp_edit,
    "wizard-speech": _wizard_page("mlx_whisper_page_id"),
    "wizard-provider": _wizard_page("provider_choice_page_id"),
    "wizard-model": _wizard_page("models_page_id"),
    "wizard-dictation": _wizard_page("dictation_page_id"),
    "wizard-mcp": _wizard_page("mcp_page_id"),
    "wizard-complete": _wizard_page("complete_page_id"),
    "splash": _splash,
    "update-available": _update_available,
    "update-progress": _update_progress,
    "update-none": _message_box(_no_update),
    "update-error": _message_box(_update_error),
    "dictation-history": _dictation_history,
    "confirmation": _confirmation,
    "diary": _diary,
    "log-viewer": _log_viewer,
    "runtime-status": _runtime_status,
    "crash-report": _captured_dialog(_crash_report),
    "unsupported-model": _captured_dialog(_unsupported_model),
    "instance-conflict": _message_box(_instance_conflict),
    "tray-menu": _tray_menu,
    "phone-access": _phone_access,
    "reply-mode-menu": _reply_mode_menu,
}


def build(name: str):
    """Build one window by name (call inside ``isolated()`` outside the test suite)."""
    load_fonts()
    return WINDOWS[name]()


def render(widget, path: Optional[Path] = None):
    """Render ``widget`` offscreen after a short settle; returns a ``QImage`` (and saves it to ``path``)."""
    from PyQt6.QtTest import QTest
    widget.show()
    QTest.qWait(60)
    image = widget.grab().toImage()
    widget.hide()
    if path is not None:
        image.save(str(path))
    return image
