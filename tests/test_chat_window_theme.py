"""Behaviour tests for the chat window's blue HUD look.

The chat window wears the orb's cyan/blue HUD palette, the one palette every
window shares. See ``chat_window.spec.md`` (Theme) and ``desktop_app.spec.md``
(Theme System). Cross-window behaviour lives in ``test_ui_theme.py``.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _retired_amber():
    """Accent values of the retired amber palette, less the ones the warning status still uses.

    They must not appear in any stylesheet: amber appears only as the warning status colour.
    """
    from desktop_app.themes import HUD_COLORS
    warning = {HUD_COLORS[key] for key in ("warning", "warning_light", "warning_glow", "warning_border")}
    retired = ("#92400e", "#d97706", "rgba(245, 158, 11, 0.15)", "rgba(245, 158, 11, 0.3)")
    return tuple(value for value in retired if value not in warning)


RETIRED_AMBER = _retired_amber()


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _hue(colour) -> float:
    return colour.hslHueF() * 360


def _open_window(monkeypatch):
    from desktop_app.chat_window import ChatWindow
    monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
    win = ChatWindow(submit_fn=lambda text: None)
    win.resize(480, 720)
    return win


def _sample(win, widget, x, y):
    """Colour of the rendered window at ``(x, y)`` in ``widget`` coordinates."""
    image = win.grab().toImage()
    point = widget.mapTo(win, widget.rect().topLeft())
    ratio = image.devicePixelRatio()
    return image.pixelColor(int((point.x() + x) * ratio), int((point.y() + y) * ratio))


def _bubble(win, kind_text):
    from PyQt6.QtWidgets import QLabel
    return next(
        w for w in win.findChildren(QLabel)
        if w.objectName() == "bubble" and w.text() == kind_text
    )


def _all_stylesheets(win) -> str:
    from PyQt6.QtWidgets import QWidget
    return win.styleSheet() + "".join(w.styleSheet() for w in win.findChildren(QWidget))


@pytest.mark.unit
class TestOrbPaletteIsSharedFromOnePlace:
    def test_orb_looks_take_their_colours_from_the_shared_palette(self):
        from desktop_app.orb_widget import OrbState, _LOOKS
        from desktop_app.themes import ORB_PALETTE
        assert _LOOKS[OrbState.IDLE].colour == ORB_PALETTE["cyan"]
        assert _LOOKS[OrbState.IDLE].accent == ORB_PALETTE["sky"]
        assert _LOOKS[OrbState.LISTENING].accent == ORB_PALETTE["blue_light"]
        assert _LOOKS[OrbState.THINKING].colour == ORB_PALETTE["indigo"]
        assert _LOOKS[OrbState.SPEAKING].colour == ORB_PALETTE["cyan_light"]
        assert _LOOKS[OrbState.SPEAKING].accent == ORB_PALETTE["blue"]
        assert _LOOKS[OrbState.OFFLINE].colour == ORB_PALETTE["slate_dark"]
        assert _LOOKS[OrbState.MUTED].accent == ORB_PALETTE["slate_light"]

    def test_orb_backdrop_is_the_shared_backdrop(self):
        from desktop_app.orb_widget import OrbWidget
        from desktop_app.themes import ORB_PALETTE
        assert OrbWidget.BG_COLOR.name() == ORB_PALETTE["backdrop"]

    def test_hud_palette_is_derived_from_the_orb(self):
        from desktop_app.themes import HUD_COLORS, ORB_PALETTE
        assert HUD_COLORS["bg_primary"] == ORB_PALETTE["backdrop"]
        assert HUD_COLORS["accent_primary"] == ORB_PALETTE["cyan"]
        assert HUD_COLORS["accent_secondary"] == ORB_PALETTE["cyan_light"]
        assert HUD_COLORS["accent_deep"] == ORB_PALETTE["blue"]
        assert HUD_COLORS["accent_indigo"] == ORB_PALETTE["indigo"]


@pytest.mark.unit
class TestHudPalette:
    @pytest.mark.parametrize("foreground, background", [
        ("text_primary", "bg_primary"), ("text_primary", "bg_secondary"),
        ("text_secondary", "bg_primary"), ("text_secondary", "bg_secondary"),
        ("text_muted", "bg_primary"), ("text_muted", "bg_secondary"),
        ("accent_secondary", "bg_secondary"), ("accent_indigo", "bg_primary"),
        ("accent_primary", "bg_primary"),
        # Text on the filled user bubble (both ends of its gradient) and buttons.
        ("bg_primary", "accent_primary"), ("bg_primary", "accent_deep"),
        ("bg_primary", "error"),
    ])
    def test_text_meets_wcag_aa(self, foreground, background):
        from desktop_app.themes import HUD_COLORS
        assert _contrast(HUD_COLORS[foreground], HUD_COLORS[background]) >= 4.5

    def test_status_colours_stay_recognisable(self):
        from PyQt6.QtGui import QColor
        from desktop_app.themes import HUD_COLORS
        assert _hue(QColor(HUD_COLORS["success"])) == pytest.approx(142, abs=25)
        assert 25 <= _hue(QColor(HUD_COLORS["warning"])) <= 50
        error_hue = _hue(QColor(HUD_COLORS["error"]))
        assert error_hue <= 15 or error_hue >= 345


@pytest.mark.unit
class TestChatWindowWearsTheHud:
    def test_surface_uses_the_orb_backdrop(self, qapp, monkeypatch):
        from PyQt6.QtGui import QColor
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QWidget
        from desktop_app.themes import HUD_COLORS
        win = _open_window(monkeypatch)
        win.show()
        QTest.qWait(30)
        surface = win.findChild(QWidget, "chatSurface")
        pixel = _sample(win, surface, 6, surface.height() // 2)
        expected = QColor(HUD_COLORS["bg_primary"])
        assert (pixel.red(), pixel.green(), pixel.blue()) == (
            expected.red(), expected.green(), expected.blue())
        win.close()

    def test_user_and_assistant_bubbles_are_clearly_distinct_and_blue(self, qapp, monkeypatch):
        from PyQt6.QtTest import QTest
        win = _open_window(monkeypatch)
        win.show()
        win._append_user("User words")
        win._append_assistant("Jarvis words")
        QTest.qWait(60)
        user = _sample(win, _bubble(win, "User words"), 6, 20)
        reply = _sample(win, _bubble(win, "Jarvis words"), 6, 20)
        assert 170 <= _hue(user) <= 235 and user.hslSaturationF() > 0.5
        assert reply.lightnessF() < 0.15
        assert user.lightnessF() - reply.lightnessF() > 0.3
        win.close()

    def test_buttons_use_cyan_for_send_and_red_for_stop(self, qapp, monkeypatch):
        from PyQt6.QtTest import QTest
        win = _open_window(monkeypatch)
        win.show()
        win._set_thinking(False)
        QTest.qWait(30)
        send = _sample(win, win.send_button, 6, 19)
        assert 170 <= _hue(send) <= 215
        win.stop_button.show()
        QTest.qWait(30)
        stop = _sample(win, win.stop_button, 6, 19)
        assert _hue(stop) <= 15 or _hue(stop) >= 345
        win.close()

    def test_avatar_glows_cyan_not_amber(self, qapp, monkeypatch):
        from PyQt6.QtTest import QTest
        from desktop_app.chat_window import JarvisOrb
        win = _open_window(monkeypatch)
        win.show()
        QTest.qWait(30)
        avatar = win.findChildren(JarvisOrb)[0]
        glow = _sample(win, avatar, avatar.width() // 2, avatar.height() // 2)
        assert 170 <= _hue(glow) <= 235
        win.close()

    def test_no_amber_remains_in_any_chat_stylesheet(self, qapp, monkeypatch):
        win = _open_window(monkeypatch)
        win._append_user("Hello")
        win._append_assistant("Hi")
        win._append_system("Notice")
        win._set_thinking(True)
        sheets = _all_stylesheets(win)
        for amber in RETIRED_AMBER:
            assert amber not in sheets, amber
        win.close()

    def test_presence_line_reflects_state_in_colour(self, qapp, monkeypatch):
        from PyQt6.QtGui import QColor
        from desktop_app.themes import HUD_COLORS
        win = _open_window(monkeypatch)
        win.show()

        def colour():
            return win._header_status.palette().windowText().color().name()

        win.set_daemon_status("running")
        assert colour() == QColor(HUD_COLORS["accent_primary"]).name()
        win._set_thinking(True)
        assert win._header_status.text() == "Typing…"
        assert colour() == QColor(HUD_COLORS["accent_indigo"]).name()
        win._set_thinking(False)
        win.set_daemon_status("stopped")
        assert colour() == QColor(HUD_COLORS["text_muted"]).name()
        win.close()

    def test_thinking_label_uses_the_orb_thinking_colour(self, qapp, monkeypatch):
        from PyQt6.QtGui import QColor
        from desktop_app.themes import HUD_COLORS
        win = _open_window(monkeypatch)
        win.show()
        win._set_thinking(True)
        assert win._status_label.text().strip() == "Jarvis is thinking…"
        assert (win._status_label.palette().windowText().color().name()
                == QColor(HUD_COLORS["accent_indigo"]).name())
        win._set_thinking(False)
        win.set_daemon_status("stopped")
        assert (win._status_label.palette().windowText().color().name()
                == QColor(HUD_COLORS["text_secondary"]).name())
        win.close()

    def test_behaviour_is_unchanged_by_the_restyle(self, qapp, monkeypatch):
        win = _open_window(monkeypatch)
        win.show()
        sent = []
        win._submit_fn = sent.append
        win.input_widget.setPlainText("Ping")
        win.send_button.click()
        assert sent == ["Ping"]
        assert win.stop_button.isVisible()
        win._on_complete("Pong")
        assert win.transcript_text() == "Ping\nPong"
        assert not win.stop_button.isVisible()
        win.close()


@pytest.mark.unit
class TestChatLookComesFromTheTheme:
    def test_only_the_mode_badge_carries_a_stylesheet_of_its_own(self, qapp, monkeypatch):
        from PyQt6.QtWidgets import QWidget
        win = _open_window(monkeypatch)
        win._append_user("Hello")
        win._append_assistant("Hi")
        win._append_system("Notice")
        styled = [w.objectName() for w in win.findChildren(QWidget) if w.styleSheet()]
        assert styled == ["modeBadge"]
        win.close()

    def test_send_stop_and_rewind_are_line_icons_not_glyphs(self, qapp, monkeypatch):
        from PyQt6.QtWidgets import QPushButton
        win = _open_window(monkeypatch)
        win._append_user("Hello")
        rewind = [b for b in win.findChildren(QPushButton) if b.objectName().startswith("rewind_")]
        for button in (win.send_button, win.stop_button, *rewind):
            assert button.text() == ""
            assert not button.icon().isNull()
            assert button.accessibleName()
        win.close()
