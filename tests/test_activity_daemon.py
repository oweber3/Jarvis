"""Daemon side of the activity log: the pause and delete requests the tray sends, and start-up wiring."""
import json
import threading

import pytest

from jarvis import daemon
from jarvis.memory import activity_runtime as runtime


@pytest.fixture
def calls(monkeypatch):
    seen = []
    done = threading.Event()

    def record(name):
        def fn(*args, **kwargs):
            seen.append((name, args))
            done.set()
            return 0
        return fn

    monkeypatch.setattr(runtime, "set_paused", lambda paused, **kw: (seen.append(("pause", paused)), done.set()))
    monkeypatch.setattr(runtime, "delete_history", record("delete"))
    return seen, done


@pytest.mark.unit
class TestActivityRequests:
    def line(self, action):
        return daemon.ACTIVITY_IPC_PREFIX + json.dumps({"action": action})

    @pytest.mark.parametrize("action,expected", [("pause", ("pause", True)), ("resume", ("pause", False))])
    def test_pause_and_resume_lines_are_applied(self, calls, action, expected):
        seen, done = calls
        assert daemon.handle_activity_stdin_line(self.line(action)) is True
        assert done.wait(2) and seen == [expected]

    def test_delete_line_deletes_history(self, calls):
        seen, done = calls
        assert daemon.handle_activity_stdin_line(self.line("delete")) is True
        assert done.wait(2) and seen[0][0] == "delete"

    def test_the_delete_never_runs_on_the_stdin_reader(self, calls):
        seen, done = calls
        reader = threading.current_thread()
        threads = []
        runtime.delete_history = lambda *a, **k: (threads.append(threading.current_thread()), done.set())
        daemon.handle_activity_stdin_line(self.line("delete"))
        assert done.wait(2) and threads and threads[0] is not reader

    @pytest.mark.parametrize("line", [
        daemon.ACTIVITY_IPC_PREFIX + "not json",
        daemon.ACTIVITY_IPC_PREFIX + json.dumps({"action": "format the disk"}),
        daemon.ACTIVITY_IPC_PREFIX + json.dumps([1, 2]),
        daemon.ACTIVITY_IPC_PREFIX + json.dumps({}),
    ])
    def test_malformed_or_unknown_requests_change_nothing_but_are_consumed(self, calls, line):
        seen, done = calls
        assert daemon.handle_activity_stdin_line(line) is True
        assert not done.wait(0.3) and seen == []

    @pytest.mark.parametrize("line", ["", "hello", "__REPLY_MODE__:{}", "__WAKE__"])
    def test_other_lines_are_not_activity_requests(self, calls, line):
        seen, done = calls
        assert daemon.handle_activity_stdin_line(line) is False
        assert seen == []


@pytest.mark.unit
class TestDaemonStartup:
    def test_the_daemon_module_offers_the_wiring_points(self):
        assert callable(daemon.set_activity_paused) and callable(daemon.delete_activity_history)
