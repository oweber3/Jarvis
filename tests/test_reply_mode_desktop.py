"""The desktop shows and switches the reply mode: tray radio items among the allowed modes, a badge on
the orb face and the chat window, and the subprocess IPC lines. Rendered offscreen only."""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.unit


class TestTrayMenu:
    def test_one_radio_item_per_mode_with_only_allowed_modes_enabled(self, qapp):
        from desktop_app.reply_mode_menu import ReplyModeMenu

        menu = ReplyModeMenu()
        menu.set_state("codex", ["local", "codex"])
        actions = menu.actions_by_mode()
        assert list(actions) == ["local", "codex", "claude"]
        assert all(a.isCheckable() for a in actions.values())
        assert [m for m, a in actions.items() if a.isChecked()] == ["codex"]
        assert {m: a.isEnabled() for m, a in actions.items()} == {"local": True, "codex": True, "claude": False}
        assert "Settings" in actions["claude"].text()
        assert "codex" in menu.menu.title().lower() or "chatgpt" in menu.menu.title().lower()

    def test_choosing_an_allowed_mode_requests_it_and_waits_for_the_daemon(self, qapp):
        from desktop_app.reply_mode_menu import ReplyModeMenu

        menu = ReplyModeMenu()
        menu.set_state("local", ["local", "claude"])
        requested = []
        menu.mode_requested.connect(requested.append)
        menu.actions_by_mode()["claude"].trigger()
        assert requested == ["claude"]
        menu.set_state("claude", ["local", "claude"])
        assert menu.actions_by_mode()["claude"].isChecked()

    def test_a_refused_switch_keeps_the_active_mode_checked(self, qapp):
        from desktop_app.reply_mode_menu import ReplyModeMenu

        menu = ReplyModeMenu()
        menu.set_state("local", ["local", "claude"])
        menu.actions_by_mode()["claude"].trigger()
        menu.set_state("local", ["local", "claude"])
        assert [m for m, a in menu.actions_by_mode().items() if a.isChecked()] == ["local"]


class TestBadge:
    def test_hidden_in_local_mode_and_named_in_a_cloud_mode(self, qapp):
        from desktop_app.mode_badge import ModeBadge

        badge = ModeBadge()
        badge.set_mode("local")
        assert badge.isHidden()
        badge.set_mode("claude")
        assert not badge.isHidden() and "Claude" in badge.text()
        badge.set_mode("codex")
        assert "ChatGPT" in badge.text()

    def test_colours_come_from_the_orb_palette(self, qapp):
        from desktop_app.mode_badge import ModeBadge
        from desktop_app.themes import HUD_COLORS

        style = ModeBadge().styleSheet()
        assert HUD_COLORS["accent_secondary"] in style and HUD_COLORS["bg_primary"] in style

    def test_face_and_chat_windows_show_the_mode(self, qapp, monkeypatch, tmp_path):
        from desktop_app.chat_window import ChatWindow
        from desktop_app.face_widget import FaceWindow

        monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
        face, chat = FaceWindow(), ChatWindow(submit_fn=lambda text: None)
        chat.resize(480, 720)
        for window in (face, chat):
            window.set_reply_mode("claude")
            assert not window.mode_badge.isHidden() and "Claude" in window.mode_badge.text()
            window.grab().save(str(tmp_path / f"{type(window).__name__}.png"))
            window.set_reply_mode("local")
            assert window.mode_badge.isHidden()
        assert (tmp_path / "FaceWindow.png").stat().st_size > 0


class TestIpcLines:
    def test_state_lines_are_parsed(self):
        from desktop_app.reply_mode_menu import parse_state_line
        from jarvis.daemon import REPLY_MODE_EVENT_PREFIX

        line = REPLY_MODE_EVENT_PREFIX + json.dumps({"type": "state", "data": {"mode": "claude",
                                                                                "enabled": ["local", "claude"]}})
        assert parse_state_line(line) == ("claude", ["local", "claude"])
        assert parse_state_line(REPLY_MODE_EVENT_PREFIX + "garbage") is None
        assert parse_state_line(REPLY_MODE_EVENT_PREFIX + json.dumps({"type": "state", "data": {
            "mode": "gemini", "enabled": ["local"]}})) is None

    def test_request_lines_match_the_daemon_protocol(self):
        from desktop_app.reply_mode_menu import request_line
        from jarvis.daemon import REPLY_MODE_IPC_PREFIX

        assert request_line("claude") == REPLY_MODE_IPC_PREFIX + json.dumps({"mode": "claude"})

    def test_state_events_are_not_shown_as_log_lines(self):
        from desktop_app.app import _should_emit_as_log
        from jarvis.daemon import REPLY_MODE_EVENT_PREFIX

        assert _should_emit_as_log(REPLY_MODE_EVENT_PREFIX + "{}") is False
        assert _should_emit_as_log("🔀 Reply mode: Claude") is True

    def test_without_a_running_daemon_the_choice_comes_from_settings(self, tmp_path, monkeypatch):
        from desktop_app.reply_mode_menu import configured_state

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"_config_version": 6, "reply_mode": "claude", "claude_enabled": True}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        assert configured_state() == ("claude", ["local", "claude"])
        path.write_text(json.dumps({"_config_version": 6, "reply_mode": "claude"}))
        assert configured_state() == ("local", ["local"])
