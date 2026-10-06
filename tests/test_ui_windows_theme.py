"""Every desktop window renders in the orb HUD look, not the retired amber one.

Each window is built offscreen from demo data (``scripts/ui_windows.py``) and
rendered with ``grab()``; the checks sample the rendered pixels and compare
them with the palette in ``desktop_app.themes``, never with hardcoded values.
Nothing is ever shown on the screen. See ``desktop_app.spec.md`` (Theme System).
"""

from __future__ import annotations

import importlib.util
import os
from collections import Counter
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]

AMBER_HUE = (20, 55)
BLUE_HUE = (170, 260)


@pytest.fixture(scope="module")
def ui_windows():
    spec = importlib.util.spec_from_file_location("ui_windows_under_test", ROOT / "scripts" / "ui_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _surface_names(image) -> Counter:
    """How many pixels of the image are each opaque colour name."""
    counts: Counter = Counter()
    step = 2
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            colour = image.pixelColor(x, y)
            if colour.alpha() == 255:
                counts[colour.name()] += 1
    return counts


def _hue_share(image) -> tuple[int, int, int]:
    """(amber pixels, blue pixels, all sampled pixels) among clearly coloured pixels."""
    amber = blue = total = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            colour = image.pixelColor(x, y)
            total += 1
            if colour.alpha() < 255 or colour.hslSaturationF() < 0.4 or colour.valueF() < 0.35:
                continue
            hue = colour.hslHueF() * 360
            if AMBER_HUE[0] <= hue <= AMBER_HUE[1]:
                amber += 1
            elif BLUE_HUE[0] <= hue <= BLUE_HUE[1]:
                blue += 1
    return amber, blue, total


def _palette_surfaces() -> set[str]:
    from PyQt6.QtGui import QColor
    from desktop_app.themes import HUD_COLORS
    keys = ("bg_primary", "bg_secondary", "bg_tertiary", "bg_card", "bg_hover")
    return {QColor(HUD_COLORS[key]).name() for key in keys}


def _window_names():
    spec = importlib.util.spec_from_file_location("ui_windows_names", ROOT / "scripts" / "ui_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.WINDOWS)


@pytest.mark.unit
@pytest.mark.parametrize("name", _window_names())
class TestEveryWindowWearsTheHud:
    def test_background_is_a_palette_surface(self, qapp, ui_windows, name):
        widget = ui_windows.build(name)
        counts = _surface_names(ui_windows.render(widget))
        dominant, _ = counts.most_common(1)[0]
        assert dominant in _palette_surfaces(), (name, dominant)

    def test_accent_is_blue_not_amber(self, qapp, ui_windows, name):
        from PyQt6.QtWidgets import QMessageBox
        widget = ui_windows.build(name)
        if isinstance(widget, QMessageBox):
            # The warning triangle is the platform's own icon, not part of the theme.
            widget.setIcon(QMessageBox.Icon.NoIcon)
        amber, blue, total = _hue_share(ui_windows.render(widget))
        # A few amber pixels are fine (a real warning line, emoji); amber chrome is not.
        assert amber <= blue or amber < total * 0.006, (name, amber, blue)


@pytest.mark.unit
class TestControlsUseTheHudAccent:
    @pytest.mark.parametrize("name", ["crash-report", "unsupported-model"])
    def test_primary_buttons_are_cyan_blue(self, qapp, ui_windows, name):
        from PyQt6.QtWidgets import QPushButton
        widget = ui_windows.build(name)
        widget.show()
        image = widget.grab().toImage()
        primary = [b for b in widget.findChildren(QPushButton) if b.objectName() == "primary"]
        assert primary, name
        button = primary[0]
        point = button.mapTo(widget, button.rect().topLeft())
        colour = image.pixelColor(point.x() + 6, point.y() + button.height() // 2)
        assert BLUE_HUE[0] <= colour.hslHueF() * 360 <= 215, colour.name()
        widget.close()

    def test_confirmation_dialog_confirm_is_the_themed_danger_button(self, qapp, ui_windows):
        from desktop_app.themes import HUD_COLORS
        widget = ui_windows.build("confirmation")
        assert widget.confirm_button.objectName() == "danger"
        assert HUD_COLORS["error"] in widget.styleSheet()
        widget.close()

    def test_log_viewer_buttons_follow_the_shared_button_style(self, qapp, ui_windows):
        from PyQt6.QtWidgets import QPushButton
        widget = ui_windows.build("log-viewer")
        for button in widget.findChildren(QPushButton):
            assert button.styleSheet() == "", button.text()
        widget.close()

    def test_reply_mode_menu_is_styled_from_the_palette(self, qapp, ui_windows):
        from desktop_app.themes import HUD_COLORS
        menu = ui_windows.build("reply-mode-menu")
        assert HUD_COLORS["bg_card"] in menu.styleSheet()
        assert HUD_COLORS["accent_glow"] in menu.styleSheet()
        menu.close()

    def test_mode_badge_comes_from_the_palette_on_the_face_and_in_chat(self, qapp, ui_windows):
        from desktop_app.themes import HUD_COLORS
        for name in ("face", "chat"):
            widget = ui_windows.build(name)
            sheet = widget.mode_badge.styleSheet()
            assert HUD_COLORS["accent_secondary"] in sheet and HUD_COLORS["bg_primary"] in sheet, name
            assert widget.mode_badge.isVisibleTo(widget), name
            widget.close()


@pytest.mark.unit
class TestSplashReusesTheOrb:
    def test_splash_renders_the_orb_in_its_thinking_colour(self, qapp, ui_windows):
        from PyQt6.QtGui import QColor
        from desktop_app.themes import ORB_PALETTE
        splash = ui_windows.build("splash")
        image = ui_windows.render(splash)
        indigo = QColor(ORB_PALETTE["indigo"])
        hits = 0
        for y in range(0, image.height(), 2):
            for x in range(0, image.width(), 2):
                colour = image.pixelColor(x, y)
                if colour.alpha() == 255 and abs(colour.hslHueF() - indigo.hslHueF()) < 0.04 \
                        and colour.hslSaturationF() > 0.3 and colour.lightnessF() > 0.3:
                    hits += 1
        assert hits > 40
        splash.close()


@pytest.mark.unit
class TestTrayContextMenu:
    def test_the_real_tray_menu_and_its_submenu_are_styled_from_the_palette(self, qapp):
        from types import SimpleNamespace
        from desktop_app import app as app_mod
        from desktop_app.themes import HUD_COLORS
        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.tray_icon = SimpleNamespace(setContextMenu=lambda menu: None)
        tray.is_listening = False
        tray.is_bundled = False
        tray.create_menu()
        assert HUD_COLORS["bg_card"] in tray.menu.styleSheet()
        assert HUD_COLORS["bg_card"] in tray.reply_mode_menu.menu.styleSheet()


@pytest.mark.unit
class TestApplicationWideTheme:
    def test_parentless_dialogs_are_themed_once_the_application_is(self, qapp):
        """Static QMessageBox calls with no parent (tray prompts) cannot be styled one by one."""
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QMessageBox
        from PyQt6.QtTest import QTest
        from desktop_app.themes import HUD_COLORS, apply_application_theme
        before = qapp.styleSheet()
        try:
            apply_application_theme(qapp)
            box = QMessageBox()
            box.setText("Reinstall GPU libraries?")
            box.show()
            QTest.qWait(30)
            image = box.grab().toImage()
            assert image.pixelColor(2, image.height() - 3).name() == QColor(HUD_COLORS["bg_primary"]).name()
            box.close()
        finally:
            qapp.setStyleSheet(before)


@pytest.mark.unit
class TestTrayMenuIcons:
    """The tray menu shows plain labels with line icons drawn from the palette."""

    def test_every_tray_item_and_reply_mode_carries_a_line_icon(self, qapp, ui_windows):
        menu = ui_windows.build("tray-menu")
        items = [action for action in menu.actions() if not action.isSeparator()]
        assert items and all(not action.icon().isNull() for action in items), [
            action.text() for action in items if action.icon().isNull()]
        reply_modes = menu._tray.reply_mode_menu.menu.actions()
        assert reply_modes and all(not action.icon().isNull() for action in reply_modes)
        menu.close()

    def test_status_dot_takes_the_status_colour(self, qapp, ui_windows):
        from PyQt6.QtGui import QColor
        from desktop_app.themes import HUD_COLORS
        menu = ui_windows.build("tray-menu")
        tray = menu._tray

        def dot():
            return tray.status_action.icon().pixmap(32, 32).toImage().pixelColor(16, 16).name()

        tray._show_listening_state(True)
        assert dot() == QColor(HUD_COLORS["success_light"]).name()
        assert tray.toggle_action.text() == "Stop Listening"
        tray._show_listening_state(False)
        assert dot() == QColor(HUD_COLORS["text_muted"]).name()
        assert tray.toggle_action.text() == "Start Listening"
        menu.close()


@pytest.mark.unit
class TestWindowsOpenWithTheHudHeader:
    @pytest.mark.parametrize("name", ["settings", "log-viewer", "dictation-history", "runtime-status",
                                      "phone-access", "confirmation", "update-available"])
    def test_window_names_itself_in_a_jarvis_eyebrow(self, qapp, ui_windows, name):
        from PyQt6.QtWidgets import QLabel
        widget = ui_windows.build(name)
        eyebrows = [label.text() for label in widget.findChildren(QLabel) if label.objectName() == "eyebrow"]
        assert any(text.startswith("Jarvis") for text in eyebrows), (name, eyebrows)
        widget.close()


@pytest.mark.unit
@pytest.mark.parametrize("name", _window_names())
def test_no_window_squeezes_wrapped_text_at_its_smallest_size(qapp, ui_windows, name, monkeypatch):
    """Shrinking a window as far as it goes never cuts wrapped text short (AGENTS.md, Qt layout rule)."""
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QLabel, QMenu
    from desktop_app.qt_worker import KeepAliveWorker
    monkeypatch.setattr(KeepAliveWorker, "start", lambda self, *a: None)
    widget = ui_windows.build(name)
    if isinstance(widget, QMenu):
        widget.close()
        pytest.skip("menus size to their items")
    widget.show()
    QTest.qWait(20)
    widget.resize(1, 1)
    QTest.qWait(30)
    squeezed = [label.text()[:40] for label in widget.findChildren(QLabel)
                if label.isVisible() and label.wordWrap() and label.text()
                and label.height() < label.heightForWidth(label.width()) - 1]
    assert squeezed == [], (name, widget.size())
    widget.close()
