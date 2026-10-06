"""The tray reaches the daemon's reply mode over stdin (subprocess mode) or directly (bundled mode), and
the daemon reports the active and allowed modes back."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from jarvis.bridge import modes

pytestmark = pytest.mark.unit


class FakeService:
    mode = "claude"

    def prepare(self):
        return None

    def is_busy(self):
        return False

    def cancel_active(self, reason):
        return False

    def close(self):
        pass


@pytest.fixture(autouse=True)
def allowed_claude():
    modes.reset()
    cfg = SimpleNamespace(reply_mode="local", codex_enabled=False, claude_enabled=True)
    modes.start(cfg, factories={"claude": lambda c: FakeService()}, save=lambda values: True)
    yield
    modes.reset()


def wait_for_mode(mode, timeout=5.0):
    done = threading.Event()
    modes.add_listener(lambda active, enabled: active == mode and done.set())
    return done.wait(timeout) or modes.active_mode() == mode


class TestStdinLine:
    def test_a_reply_mode_line_switches_off_the_reader_thread(self):
        from jarvis import daemon

        line = daemon.REPLY_MODE_IPC_PREFIX + json.dumps({"mode": "claude"})
        assert daemon.handle_reply_mode_stdin_line(line) is True
        assert wait_for_mode("claude")

    @pytest.mark.parametrize("payload", ["not json", json.dumps(["claude"]), json.dumps({"mode": 3})])
    def test_malformed_requests_are_consumed_and_change_nothing(self, payload):
        from jarvis import daemon

        assert daemon.handle_reply_mode_stdin_line(daemon.REPLY_MODE_IPC_PREFIX + payload) is True
        assert modes.active_mode() == "local"

    def test_a_mode_that_is_not_allowed_changes_nothing(self):
        from jarvis import daemon

        daemon.handle_reply_mode_stdin_line(daemon.REPLY_MODE_IPC_PREFIX + json.dumps({"mode": "codex"}))
        assert modes.active_mode() == "local"

    def test_other_lines_are_left_for_other_handlers(self):
        from jarvis import daemon

        assert daemon.handle_reply_mode_stdin_line(daemon.WAKE_IPC_PREFIX) is False
        assert daemon.handle_reply_mode_stdin_line('__CHAT_QUERY__:{"text":"use claude"}') is False


class TestBundledAndEvents:
    def test_set_reply_mode_switches_directly(self):
        from jarvis import daemon

        result = daemon.set_reply_mode("claude")
        assert result.ok and modes.active_mode() == "claude"

    def test_state_events_carry_the_mode_and_the_allowed_modes(self, capsys):
        from jarvis import daemon

        modes.add_listener(daemon._emit_reply_mode_state)
        daemon.set_reply_mode("claude")
        lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(daemon.REPLY_MODE_EVENT_PREFIX)]
        event = json.loads(lines[-1][len(daemon.REPLY_MODE_EVENT_PREFIX):])
        assert event == {"type": "state", "data": {"mode": "claude", "enabled": ["local", "claude"]}}
