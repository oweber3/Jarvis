"""Nothing the desktop app draws on screen carries an emoji.

Windows, dialogs, wizard pages, the tray menu and its submenus, the Settings
metadata and the memory viewer page use plain text, and line icons drawn from
the palette where an icon helps. Console and log output keep their emojis
(AGENTS.md), so the log viewer's log text is not checked. See
``desktop_app.spec.md`` (Theme System).

Windows are built offscreen from demo data (``scripts/ui_windows.py``); a
source check covers on-screen strings that only appear in states the demo
windows do not reach (a status that changes later, an error message).
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "desktop_app"

# Extended pictographic characters (emoji and the dingbats and symbols drawn as
# emoji), plus the emoji presentation selector, joiner and keycap. Plain
# typographic arrows (← ↑ → ↓), bullets and dashes are text, not emoji.
EMOJI = re.compile(
    "["
    "‼⁉™ℹ↔-↙↩↪⌚⌛⌨⎈⏏"
    "⏩-⏳⏸-⏺Ⓜ▪▫▶◀◻-◾"
    "☀-➿⤴⤵⬅-⬇⬛⬜⭐⭕〰〽㊗㊙"
    "️‍⃣"
    "\U0001f000-\U0001faff"
    "]"
)


def _emoji_in(texts):
    return sorted({text for text in texts if text and EMOJI.search(text)})


def _ui_windows():
    spec = importlib.util.spec_from_file_location("ui_windows_no_emoji", ROOT / "scripts" / "ui_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _menu_texts(menu):
    texts = [menu.title()]
    for action in menu.actions():
        texts += [action.text(), action.toolTip() if action.toolTip() != action.text().replace("&", "") else ""]
        if action.menu() is not None:
            texts += _menu_texts(action.menu())
    return texts


def _on_screen_texts(widget):
    """Every string the widget and its children draw: titles, labels, buttons, items, menus, tooltips."""
    from PyQt6.QtWidgets import (
        QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QListWidget, QMenu,
        QPlainTextEdit, QTabBar, QTextBrowser, QWidget, QWizard,
    )
    texts = []
    for child in [widget, *widget.findChildren(QWidget)]:
        texts += [child.windowTitle(), child.toolTip()]
        if isinstance(child, QLabel):
            texts.append(child.text())
        elif isinstance(child, QAbstractButton):
            texts.append(child.text())
        elif isinstance(child, QGroupBox):
            texts.append(child.title())
        elif isinstance(child, QTabBar):
            texts += [child.tabText(i) for i in range(child.count())]
        elif isinstance(child, QListWidget):
            texts += [child.item(i).text() for i in range(child.count())]
        elif isinstance(child, QComboBox):
            texts += [child.itemText(i) for i in range(child.count())]
        elif isinstance(child, (QLineEdit, QPlainTextEdit)):
            texts.append(child.placeholderText())
        elif isinstance(child, QTextBrowser):
            texts.append(child.toPlainText())
        if isinstance(child, QMenu):
            texts += _menu_texts(child)
        if isinstance(child, QWizard):
            buttons = QWizard.WizardButton
            texts += [child.buttonText(button) for button in (
                buttons.BackButton, buttons.NextButton, buttons.CommitButton, buttons.FinishButton,
                buttons.CancelButton, buttons.HelpButton)]
            for page_id in child.pageIds():
                page = child.page(page_id)
                texts += [page.title(), page.subTitle()]
    return texts


def _real_tray_menu():
    from desktop_app import app as app_mod
    tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
    tray.tray_icon = SimpleNamespace(setContextMenu=lambda menu: None)
    tray.is_listening = False
    tray.is_bundled = False
    tray.create_menu()
    tray.activity_menu.refresh_from_settings()
    tray.menu._tray = tray
    return tray.menu


def _phone_access(enabled: bool):
    def build():
        from desktop_app import phone_access_dialog
        cfg = SimpleNamespace(remote_access_enabled=enabled, remote_access_port=8765,
                              db_path=str(Path(os.environ["XDG_DATA_HOME"]) / "jarvis.db"))
        with patch.object(phone_access_dialog, "phone_links", lambda port: [f"http://192.168.1.20:{port}/"]):
            return phone_access_dialog.PhoneAccessDialog(cfg)
    return build


def _other_wizard_pages(ui_windows):
    """The wizard pages ``ui_windows`` does not already build."""
    names = ("welcome", "openai_compat", "ollama_install", "ollama_server", "search_providers", "location")
    return {f"wizard-{name}": ui_windows._wizard_page(f"{name}_page_id") for name in names}


def _screens():
    ui_windows = _ui_windows()
    screens = dict(ui_windows.WINDOWS)
    screens["tray-menu"] = _real_tray_menu
    screens.update(_other_wizard_pages(ui_windows))
    screens["phone-access-off"] = _phone_access(False)
    screens["phone-access-on"] = _phone_access(True)
    return screens


SCREENS = _screens()


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(SCREENS))
def test_no_window_draws_an_emoji(qapp, name, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QWidget
    from desktop_app.qt_worker import KeepAliveWorker
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Wizard pages start their status checks when shown; the checks are not needed to read the page.
    monkeypatch.setattr(KeepAliveWorker, "start", lambda self, *a: None)
    ui_windows = _ui_windows()
    ui_windows.load_fonts()
    widget = SCREENS[name]()
    try:
        if isinstance(widget, QWidget):
            widget.show()
        assert _emoji_in(_on_screen_texts(widget)) == []
    finally:
        widget.close()


@pytest.mark.unit
def test_tray_menu_states_carry_no_emoji(qapp):
    """The listening toggle and status line change with the daemon; every state is plain text."""
    from desktop_app import app as app_mod
    menu = _real_tray_menu()
    tray = menu._tray
    texts = []
    for listening in (True, False):
        app_mod.JarvisSystemTray._show_listening_state(tray, listening)
        texts += [tray.toggle_action.text(), tray.status_action.text()]
    assert all(texts)
    assert _emoji_in(texts) == []


@pytest.mark.unit
def test_settings_metadata_carries_no_emoji():
    from desktop_app.settings_window import CATEGORIES, FIELD_METADATA
    texts = [label for _, label in CATEGORIES]
    for field in FIELD_METADATA:
        texts += [value for value in vars(field).values() if isinstance(value, str)]
        for option in getattr(field, "options", None) or []:
            texts += [str(part) for part in (option if isinstance(option, (list, tuple)) else (option,))]
    assert _emoji_in(texts) == []


@pytest.mark.unit
def test_mcp_catalogue_names_carry_no_emoji():
    from desktop_app.mcp_catalogue import CATALOGUE
    texts = [text for entry in CATALOGUE for text in (entry.display_name, entry.description)]
    assert _emoji_in(texts) == []


@pytest.mark.unit
def test_memory_viewer_page_carries_no_emoji():
    from desktop_app import memory_viewer
    page = memory_viewer.app.test_client().get("/").get_data(as_text=True)
    assert _emoji_in(page.splitlines()) == []


# Qt calls and helpers whose string arguments end up on screen.
_ON_SCREEN_CALLS = {
    "QLabel", "QPushButton", "QAction", "QRadioButton", "QCheckBox", "QGroupBox", "QMenu", "QListWidgetItem",
    "setText", "setWindowTitle", "setTitle", "setSubTitle", "setPlaceholderText", "setToolTip", "setStatusTip",
    "setButtonText", "addItem", "addItems", "addTab", "addButton", "setInformativeText", "information", "warning",
    "critical", "question", "about", "getText", "_create_status_row", "_update_status_row", "_plain",
    "set_status", "set_caption", "hud_heading", "eyebrow",
}


def _strings(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value


def _on_screen_literals(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in _ON_SCREEN_CALLS:
            for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                for text in _strings(arg):
                    yield node.lineno, text
        for keyword in node.keywords:
            if keyword.arg in ("label", "display_name", "title", "text", "tooltip"):
                for text in _strings(keyword.value):
                    yield node.lineno, text


@pytest.mark.unit
@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_on_screen_strings_in_the_source_carry_no_emoji(path):
    hits = [f"{path.name}:{line}: {text!r}" for line, text in _on_screen_literals(path) if EMOJI.search(text)]
    assert hits == []


@pytest.mark.unit
def test_the_detector_tells_emoji_from_typographic_text():
    assert EMOJI.search("📱 Phone Access")
    assert EMOJI.search("⚙️ Settings")
    assert EMOJI.search("✅ Saved")
    assert EMOJI.search("⏸️ Pause")
    assert not EMOJI.search("Next →  ← Back  · • … – Settings > Phone Access")
