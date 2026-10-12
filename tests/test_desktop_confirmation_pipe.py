"""Desktop confirmations when the daemon is a subprocess of the desktop app.

The daemon asks the desktop app to show the dialog with a ``__CONFIRM__:`` line on its stdout and
reads the answer as a ``__CONFIRM_ANSWER__:`` line on its stdin, the pipes the chat already uses.
No answer is a refusal. A daemon the desktop app did not start keeps refusing dialog actions.
See ``desktop_app.spec.md`` (Action Confirmation Dialog) and ``chat_window.spec.md``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.tools.confirmation as confirmation
from jarvis import daemon
from jarvis.tools.base import Tool
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    SafetyTier,
    get_confirmation_store,
    get_dialog_callback,
    set_dialog_callback,
    set_result_handler,
)
from jarvis.tools.confirmation_pipe import (
    CONFIRM_ANSWER_IPC_PREFIX,
    CONFIRM_IPC_PREFIX,
    PipeDialogPresenter,
)
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult

SRC = Path(__file__).resolve().parent.parent / "src"


class Cfg:
    windows_tools_enabled = True
    windows_app_aliases = {}
    voice_debug = False


class UninstallTool(Tool):
    def __init__(self):
        self.runs: list[int] = []
        self.ran = threading.Event()

    @property
    def name(self):
        return "pipeUninstallTool"

    @property
    def description(self):
        return "test"

    @property
    def inputSchema(self):
        return {"type": "object", "properties": {"target": {"type": "string"}}}

    def classify_safety(self, args, cfg):
        return ConfirmationRequest(self.name, SafetyTier.CONFIRM_DIALOG, "uninstall",
                                   str((args or {}).get("target", "Example App")), dict(args or {}),
                                   consequence="It will be removed.")

    def run(self, args, context):
        self.runs.append(threading.get_ident())
        self.ran.set()
        return ToolExecutionResult(success=True, reply_text="Uninstalled Example App.")


class DaemonOutput:
    """The daemon's stdout as the desktop app sees it: one line per event."""

    def __init__(self):
        self.lines: list[str] = []
        self.event = threading.Event()

    def __call__(self, line: str) -> None:
        self.lines.append(line)
        self.event.set()

    def events(self):
        return [json.loads(line[len(CONFIRM_IPC_PREFIX):]) for line in self.lines
                if line.startswith(CONFIRM_IPC_PREFIX)]


@pytest.fixture(autouse=True)
def clean_state():
    store = get_confirmation_store()
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)
    set_result_handler(None, origin=confirmation.ORIGIN_CHAT)
    daemon._chat_result_listeners.clear()
    yield
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)
    set_result_handler(None, origin=confirmation.ORIGIN_CHAT)
    daemon._chat_result_listeners.clear()


@pytest.fixture
def tool():
    t = UninstallTool()
    BUILTIN_TOOLS[t.name] = t
    yield t
    BUILTIN_TOOLS.pop(t.name, None)


@pytest.fixture
def output():
    return DaemonOutput()


@pytest.fixture
def presenter(output):
    p = PipeDialogPresenter(output)
    set_dialog_callback(p)
    return p


def _request(quiet=True):
    return run_tool_with_retries(
        db=None, cfg=Cfg(), tool_name="pipeUninstallTool", tool_args={"target": "Example App"},
        system_prompt="", original_prompt="", redacted_text="", quiet=quiet,
    )


def _answer(request_id, approved) -> str:
    return CONFIRM_ANSWER_IPC_PREFIX + json.dumps({"id": request_id, "approved": approved})


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Daemon side: asking over stdout, answering over stdin
# ---------------------------------------------------------------------------

class TestDaemonAsksTheDesktop:
    def test_a_dialog_action_asks_the_desktop_app_to_show_the_dialog(self, presenter, output, tool):
        result = _request()

        assert result.success is False
        assert "confirm on your desktop" in (result.reply_text or "").lower()
        [opened] = output.events()
        assert opened["type"] == "open"
        assert opened["data"]["action"] == "uninstall"
        assert opened["data"]["target"] == "Example App"
        assert opened["data"]["consequence"] == "It will be removed."
        assert tool.runs == []

    def test_yes_runs_the_action_once(self, presenter, output, tool):
        _request()
        request_id = output.events()[0]["data"]["id"]

        assert daemon.handle_confirm_answer_stdin_line(_answer(request_id, True)) is True
        assert daemon.handle_confirm_answer_stdin_line(_answer(request_id, True)) is True

        assert tool.ran.wait(3)
        time.sleep(0.1)
        assert len(tool.runs) == 1

    def test_no_does_not_run_the_action(self, presenter, output, tool):
        _request()
        request_id = output.events()[0]["data"]["id"]

        daemon.handle_confirm_answer_stdin_line(_answer(request_id, False))

        time.sleep(0.2)
        assert tool.runs == []
        assert not get_confirmation_store().has_pending()

    def test_no_answer_expires_closes_the_dialog_and_runs_nothing(self, presenter, output, tool, monkeypatch):
        monkeypatch.setattr(confirmation, "DIALOG_TIMEOUT_SEC", 0.05)
        _request()
        request_id = output.events()[0]["data"]["id"]

        assert _wait(lambda: any(e["type"] == "close" for e in output.events()))
        assert output.events()[-1] == {"type": "close", "data": {"id": request_id}}
        daemon.handle_confirm_answer_stdin_line(_answer(request_id, True))  # a late click
        time.sleep(0.2)
        assert tool.runs == []

    @pytest.mark.parametrize("approved", ["yes", 1, None, "true"])
    def test_an_answer_that_is_not_a_plain_yes_never_approves(self, presenter, output, tool, approved):
        _request()
        request_id = output.events()[0]["data"]["id"]

        daemon.handle_confirm_answer_stdin_line(_answer(request_id, approved))

        time.sleep(0.2)
        assert tool.runs == []

    @pytest.mark.parametrize("line", [
        CONFIRM_ANSWER_IPC_PREFIX + "not json",
        CONFIRM_ANSWER_IPC_PREFIX + json.dumps(["approved"]),
        CONFIRM_ANSWER_IPC_PREFIX + json.dumps({"id": "someone-else", "approved": True}),
        CONFIRM_ANSWER_IPC_PREFIX + json.dumps({"id": ["x"], "approved": True}),
    ])
    def test_malformed_or_unknown_answers_are_consumed_and_ignored(self, presenter, output, tool, line):
        _request()

        assert daemon.handle_confirm_answer_stdin_line(line) is True

        time.sleep(0.2)
        assert tool.runs == []
        assert get_confirmation_store().has_pending()

    def test_other_lines_are_not_answers(self):
        assert daemon.handle_confirm_answer_stdin_line("__CHAT_CANCEL__") is False

    def test_a_failing_answer_never_takes_the_stdin_monitor_down(self, output):
        presenter = PipeDialogPresenter(output)

        def broken(_approved):
            raise RuntimeError("boom")

        handle = presenter(SimpleNamespace(id="r1", action="a", target="t", consequence=None, tool_name="x"), broken)
        assert handle is not None
        assert presenter.handle_answer_line(_answer("r1", True)) is True

    @pytest.mark.parametrize("line, handler", [
        ("__CHAT_CANCEL__", "cancel_active_chat_query"),
        ("__WAKE__", "toggle_manual_wake"),
        ("__CHAT_NEW_SESSION__", "new_chat_session"),
    ])
    def test_the_other_requests_still_reach_their_handlers(self, monkeypatch, line, handler):
        seen = []
        monkeypatch.setattr(daemon, handler, lambda *a, **k: seen.append(line))
        assert daemon.handle_ipc_stdin_line(line) is True
        assert seen == [line]

    def test_a_typed_query_still_reaches_the_reply_engine(self, monkeypatch):
        seen = []
        monkeypatch.setattr(daemon, "submit_text_query", lambda text, **k: seen.append(text))
        assert daemon.handle_ipc_stdin_line(daemon.CHAT_QUERY_IPC_PREFIX + json.dumps({"text": "hello"})) is True
        assert seen == ["hello"]

    def test_unknown_lines_are_ignored(self):
        assert daemon.handle_ipc_stdin_line("just some text") is False

    def test_the_stdin_monitor_routes_answers(self, presenter, output, tool):
        _request()
        request_id = output.events()[0]["data"]["id"]

        assert daemon.handle_ipc_stdin_line(_answer(request_id, True)) is True

        assert tool.ran.wait(3)

    def test_a_newer_request_closes_the_older_dialog(self, presenter, output, tool):
        _request()
        first = output.events()[0]["data"]["id"]
        _request()

        assert {"type": "close", "data": {"id": first}} in output.events()
        daemon.handle_confirm_answer_stdin_line(_answer(first, True))
        time.sleep(0.2)
        assert tool.runs == []

    def test_logs_never_carry_the_target(self, presenter, output, tool, monkeypatch):
        logged = []
        monkeypatch.setattr("jarvis.tools.confirmation_pipe.debug_log", lambda msg, *a, **k: logged.append(msg))
        _request()
        request_id = output.events()[0]["data"]["id"]
        daemon.handle_confirm_answer_stdin_line(_answer(request_id, False))
        assert logged
        assert not any("Example App" in message for message in logged)


class TestWhoGetsTheDialog:
    def test_a_daemon_started_by_the_desktop_app_installs_the_pipe_presenter(self, monkeypatch, tool):
        monkeypatch.setenv("JARVIS_STDIN_IPC", "1")
        monkeypatch.delattr(sys, "frozen", raising=False)
        try:
            daemon.install_desktop_confirmations()
            assert isinstance(get_dialog_callback(), PipeDialogPresenter)
        finally:
            daemon.uninstall_desktop_confirmations()
        assert get_dialog_callback() is None

    def test_a_headless_daemon_still_refuses_dialog_actions(self, monkeypatch, tool):
        monkeypatch.delenv("JARVIS_STDIN_IPC", raising=False)
        daemon.install_desktop_confirmations()

        result = _request()

        assert get_dialog_callback() is None
        assert result.success is False
        assert "no desktop interface is active" in (result.error_message or "")
        assert tool.runs == []

    def test_the_bundled_app_keeps_its_own_dialog(self, monkeypatch):
        monkeypatch.setenv("JARVIS_STDIN_IPC", "1")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        own = lambda request, resolve: None  # noqa: E731
        set_dialog_callback(own)

        daemon.install_desktop_confirmations()

        assert get_dialog_callback() is own


class TestTypedRequestsHearTheOutcome:
    def test_a_confirmed_typed_action_reports_back_as_a_chat_event(self, tool, monkeypatch, capsys):
        monkeypatch.setenv("JARVIS_STDIN_IPC", "1")
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(daemon, "_global_dialogue_memory", None)
        daemon.install_chat_result_delivery()
        daemon.install_desktop_confirmations()
        seen = []

        def events(prefix):
            seen.extend(capsys.readouterr().out.splitlines())
            return [json.loads(line[len(prefix):]) for line in seen if line.startswith(prefix)]

        try:
            _request(quiet=True)
            request_id = events(CONFIRM_IPC_PREFIX)[0]["data"]["id"]
            daemon.handle_ipc_stdin_line(_answer(request_id, True))
            assert tool.ran.wait(3)
            assert _wait(lambda: [e for e in events(daemon.CHAT_IPC_PREFIX) if e["type"] == "confirmed"])
            assert [e for e in events(daemon.CHAT_IPC_PREFIX) if e["type"] == "confirmed"] == [
                {"type": "confirmed", "data": "Uninstalled Example App."}]
        finally:
            daemon.uninstall_desktop_confirmations()


# ---------------------------------------------------------------------------
# Desktop side: showing the dialog and writing the answer
# ---------------------------------------------------------------------------

class FakeDialogs:
    """Stands in for the Qt dialog: records what it was asked to show."""

    def __init__(self, refuse=False):
        self.shown = []
        self.refuse = refuse

    def __call__(self, request, resolve):
        if self.refuse:
            return None
        handle = SimpleNamespace(closed=False)
        handle.close = lambda: setattr(handle, "closed", True)
        self.shown.append(SimpleNamespace(request=request, resolve=resolve, handle=handle))
        return handle


class Pipe:
    """The daemon's stdin as the desktop app writes it."""

    def __init__(self, broken=False):
        self.lines: list[str] = []
        self.broken = broken

    def __call__(self, line: str) -> None:
        if self.broken:
            raise BrokenPipeError("daemon gone")
        self.lines.append(line)


def _open_line(request_id="abc123", action="uninstall", target="Example App", consequence=None):
    data = {"id": request_id, "action": action, "target": target, "consequence": consequence}
    return CONFIRM_IPC_PREFIX + json.dumps({"type": "open", "data": data})


class TestDesktopRelay:
    @pytest.fixture
    def relay_parts(self):
        from desktop_app.confirmation_dialog import SubprocessConfirmationRelay
        dialogs, pipe = FakeDialogs(), Pipe()
        return SimpleNamespace(relay=SubprocessConfirmationRelay(pipe, presenter=dialogs), dialogs=dialogs, pipe=pipe)

    def test_an_open_event_shows_the_dialog_with_the_request(self, relay_parts):
        assert relay_parts.relay.handle_line(_open_line(consequence="It will be removed.")) is True
        [shown] = relay_parts.dialogs.shown
        assert (shown.request.action, shown.request.target, shown.request.consequence) == (
            "uninstall", "Example App", "It will be removed.")
        assert relay_parts.pipe.lines == []

    @pytest.mark.parametrize("approved", [True, False])
    def test_the_answer_goes_back_to_the_daemon_once(self, relay_parts, approved):
        relay_parts.relay.handle_line(_open_line())
        resolve = relay_parts.dialogs.shown[0].resolve

        resolve(approved)
        resolve(approved)

        assert relay_parts.pipe.lines == [_answer("abc123", approved)]

    def test_a_close_event_dismisses_the_dialog_without_answering(self, relay_parts):
        relay_parts.relay.handle_line(_open_line())
        relay_parts.relay.handle_line(CONFIRM_IPC_PREFIX + json.dumps({"type": "close", "data": {"id": "abc123"}}))

        assert relay_parts.dialogs.shown[0].handle.closed is True
        relay_parts.dialogs.shown[0].resolve(True)  # a click racing the close
        assert relay_parts.pipe.lines == []

    def test_a_daemon_that_goes_away_closes_every_dialog_without_answering(self, relay_parts):
        relay_parts.relay.handle_line(_open_line("one"))
        relay_parts.relay.handle_line(_open_line("two"))

        relay_parts.relay.close_all()

        assert all(s.handle.closed for s in relay_parts.dialogs.shown)
        relay_parts.dialogs.shown[0].resolve(True)
        assert relay_parts.pipe.lines == []

    def test_a_dialog_that_cannot_be_shown_is_declined_at_once(self):
        from desktop_app.confirmation_dialog import SubprocessConfirmationRelay
        pipe = Pipe()
        relay = SubprocessConfirmationRelay(pipe, presenter=FakeDialogs(refuse=True))

        relay.handle_line(_open_line())

        assert pipe.lines == [_answer("abc123", False)]

    @pytest.mark.parametrize("line", [
        CONFIRM_IPC_PREFIX + "not json",
        CONFIRM_IPC_PREFIX + json.dumps({"type": "open", "data": {"id": 5, "action": "x"}}),
        CONFIRM_IPC_PREFIX + json.dumps({"type": "open", "data": {"id": "a", "action": ["x"]}}),
        CONFIRM_IPC_PREFIX + json.dumps({"type": "open", "data": "x"}),
        CONFIRM_IPC_PREFIX + json.dumps({"type": "explode", "data": {}}),
    ])
    def test_malformed_events_show_nothing(self, relay_parts, line):
        assert relay_parts.relay.handle_line(line) is True
        assert relay_parts.dialogs.shown == []

    def test_other_lines_are_not_confirmation_events(self, relay_parts):
        assert relay_parts.relay.handle_line("__CHAT__:{}") is False

    def test_a_duplicate_open_keeps_the_first_dialog(self, relay_parts):
        relay_parts.relay.handle_line(_open_line())
        relay_parts.relay.handle_line(_open_line())
        assert len(relay_parts.dialogs.shown) == 1

    def test_a_relay_whose_daemon_went_away_shows_nothing_more(self, relay_parts):
        relay_parts.relay.close_all()
        assert relay_parts.relay.handle_line(_open_line()) is True
        assert relay_parts.dialogs.shown == []

    def test_a_dialog_the_daemon_never_closes_is_closed_unanswered_after_the_timeout(self):
        from desktop_app.confirmation_dialog import SubprocessConfirmationRelay
        dialogs, pipe = FakeDialogs(), Pipe()
        relay = SubprocessConfirmationRelay(pipe, presenter=dialogs, timeout_sec=0.05)
        relay.handle_line(_open_line())
        assert _wait(lambda: dialogs.shown[0].handle.closed)
        dialogs.shown[0].resolve(True)
        assert pipe.lines == []

    def test_a_broken_pipe_is_not_an_approval_and_does_not_raise(self):
        from desktop_app.confirmation_dialog import SubprocessConfirmationRelay
        dialogs = FakeDialogs()
        relay = SubprocessConfirmationRelay(Pipe(broken=True), presenter=dialogs)
        relay.handle_line(_open_line())
        dialogs.shown[0].resolve(True)  # must not raise


class TestRoundTrip:
    """Daemon presenter -> desktop relay -> daemon answer handler, with the real confirmation store."""

    @pytest.fixture
    def wired(self, tool):
        from desktop_app.confirmation_dialog import SubprocessConfirmationRelay
        dialogs = FakeDialogs()
        relay = SubprocessConfirmationRelay(daemon.handle_ipc_stdin_line, presenter=dialogs)
        set_dialog_callback(PipeDialogPresenter(relay.handle_line))
        return SimpleNamespace(dialogs=dialogs, relay=relay)

    def test_confirm_in_the_desktop_dialog_runs_the_action(self, wired, tool):
        _request()
        wired.dialogs.shown[0].resolve(True)
        assert tool.ran.wait(3)
        assert len(tool.runs) == 1

    def test_cancel_in_the_desktop_dialog_runs_nothing(self, wired, tool):
        _request()
        wired.dialogs.shown[0].resolve(False)
        time.sleep(0.2)
        assert tool.runs == []
        assert not get_confirmation_store().has_pending()

    def test_expiry_closes_the_desktop_dialog(self, wired, tool, monkeypatch):
        monkeypatch.setattr(confirmation, "DIALOG_TIMEOUT_SEC", 0.05)
        _request()
        assert _wait(lambda: wired.dialogs.shown[0].handle.closed)
        wired.dialogs.shown[0].resolve(True)
        time.sleep(0.2)
        assert tool.runs == []


# ---------------------------------------------------------------------------
# Across a real process boundary
# ---------------------------------------------------------------------------

CHILD = textwrap.dedent(
    """
    import os, sys, threading
    os.environ["JARVIS_STDIN_IPC"] = "1"
    from jarvis import daemon
    from jarvis.tools.base import Tool
    from jarvis.tools.confirmation import ConfirmationRequest, SafetyTier
    from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
    from jarvis.tools.types import ToolExecutionResult

    ran = threading.Event()

    class Uninstall(Tool):
        name = "childUninstall"
        description = "test"
        inputSchema = {"type": "object", "properties": {}}
        def classify_safety(self, args, cfg):
            return ConfirmationRequest(self.name, SafetyTier.CONFIRM_DIALOG, "uninstall", "Example App", {})
        def run(self, args, context):
            ran.set()
            return ToolExecutionResult(success=True, reply_text="done")

    class Cfg:
        windows_tools_enabled = True
        windows_app_aliases = {}
        voice_debug = False

    BUILTIN_TOOLS["childUninstall"] = Uninstall()
    daemon.install_desktop_confirmations()
    result = run_tool_with_retries(db=None, cfg=Cfg(), tool_name="childUninstall", tool_args={},
                                   system_prompt="", original_prompt="", redacted_text="")
    print("REPLY " + str(bool(result.reply_text)), flush=True)
    line = sys.stdin.readline()
    daemon.handle_ipc_stdin_line(line.strip())
    print("RAN" if ran.wait(3) else "NOT RAN", flush=True)
    """
)


@pytest.mark.parametrize("approved, expected", [(True, "RAN"), (False, "NOT RAN")])
def test_a_daemon_subprocess_gets_its_answer_over_the_pipes(approved, expected, tmp_path):
    from desktop_app.confirmation_dialog import SubprocessConfirmationRelay
    env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8")
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
    try:
        def write(line):
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

        dialogs = FakeDialogs()
        relay = SubprocessConfirmationRelay(write, presenter=dialogs)
        out = []
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip("\n")
            out.append(line)
            if relay.handle_line(line):
                dialogs.shown[-1].resolve(approved)
            if line in ("RAN", "NOT RAN"):
                break
        assert "REPLY True" in out
        assert out[-1] == expected
    finally:
        proc.kill()
        proc.wait(10)


# ---------------------------------------------------------------------------
# The tray: daemon output in, answers out
# ---------------------------------------------------------------------------

class TestTrayWiring:
    @pytest.fixture
    def tray(self, qapp, monkeypatch):
        from unittest.mock import MagicMock
        import desktop_app.app as app_mod
        import desktop_app.confirmation_dialog as dialog_mod

        dialogs = FakeDialogs()
        monkeypatch.setattr(dialog_mod, "show_desktop_confirmation_dialog", dialogs)
        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.is_bundled = False
        tray.daemon_process = None
        tray.daemon_thread = None
        tray.is_listening = False
        tray.chat_window = None
        tray.web_chat_window = None
        tray._chat_submit_fn = None
        tray._chat_cancel_fn = None
        tray._chat_control_fn = None
        tray.log_reader_threads = []
        tray.log_signals = MagicMock()
        tray.tray_icon = MagicMock()
        tray.app = MagicMock()
        tray.update_icon = MagicMock()
        tray._show_listening_state = MagicMock()
        tray._daemon_stop_expected = False
        tray._web_chat_port = None
        monkeypatch.setattr(app_mod, "web_chat_port_if_enabled", lambda: None)
        tray._confirm_ipc_signals = app_mod.ConfirmIpcSignals()
        tray._confirm_ipc_signals.line_received.connect(tray._on_confirm_ipc_line)
        proc = MagicMock()
        proc.stdin = io.StringIO()
        proc.stdout = io.StringIO("")  # the real log reader started below reads nothing and ends
        monkeypatch.setattr(app_mod.subprocess, "Popen", MagicMock(return_value=proc))
        tray.start_daemon()
        tray.log_reader_threads[-1].join(5)
        return SimpleNamespace(tray=tray, dialogs=dialogs, proc=proc)

    def test_a_request_mixed_into_another_output_line_is_still_handled_and_kept_out_of_the_log(self, tray, qapp):
        tray.proc.stdout = io.StringIO("🎤 Listening..." + _open_line() + "\n")
        tray.tray.daemon_process = tray.proc

        tray.tray._read_daemon_logs()
        assert _wait(lambda: (qapp.processEvents(), tray.dialogs.shown)[1])

        logged = "".join(call.args[0] for call in tray.tray.log_signals.new_log.emit.call_args_list)
        assert "Example App" not in logged

    def test_requests_from_a_daemon_that_was_replaced_never_reach_the_new_one(self, tray, qapp):
        old_relay = tray.tray._confirm_relay
        old_proc = tray.proc
        old_proc.stdout = io.StringIO(_open_line("old") + "\n")
        tray.tray.daemon_process = old_proc
        reader_relay = tray.tray._confirm_relay
        tray.tray._confirm_relay = SimpleNamespace(handle_line=lambda line: pytest.fail("reached the new daemon"),
                                                   close_all=lambda: None)
        tray.tray._read_daemon_logs(relay=reader_relay)
        _wait(lambda: (qapp.processEvents(), tray.dialogs.shown)[1])
        assert reader_relay is old_relay
        assert [s.request.id for s in tray.dialogs.shown] == ["old"]

    def test_a_daemon_request_shows_the_dialog_and_the_answer_reaches_its_stdin(self, tray):
        tray.tray._on_confirm_ipc_line(_open_line() + "\n")

        [shown] = tray.dialogs.shown
        assert shown.request.target == "Example App"
        shown.resolve(True)
        assert tray.proc.stdin.getvalue() == _answer("abc123", True) + "\n"

    def test_the_log_reader_hands_requests_to_the_gui_thread_and_keeps_them_out_of_the_log(self, tray, qapp):
        tray.proc.stdout = io.StringIO("🚀 hello\n" + _open_line() + "\n")
        tray.tray.daemon_process = tray.proc

        tray.tray._read_daemon_logs()
        assert _wait(lambda: (qapp.processEvents(), tray.dialogs.shown)[1])

        logged = [call.args[0] for call in tray.tray.log_signals.new_log.emit.call_args_list]
        assert any("hello" in line for line in logged)
        assert not any(CONFIRM_IPC_PREFIX in line for line in logged)

    @pytest.mark.parametrize("status", ["stopping", "stopped", "crashed"])
    def test_a_daemon_that_goes_away_closes_its_dialogs_unanswered(self, tray, status):
        tray.tray._on_confirm_ipc_line(_open_line())
        before = tray.proc.stdin.getvalue()

        tray.tray._set_chat_daemon_status(status)

        assert tray.dialogs.shown[0].handle.closed is True
        tray.dialogs.shown[0].resolve(True)
        assert tray.proc.stdin.getvalue() == before


class TestQtChatHearsTheOutcome:
    def test_an_outcome_with_no_chat_window_open_creates_no_window(self, qapp):
        import desktop_app.app as app_mod
        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.chat_window = None
        tray._chat_submit_fn = None
        tray.is_listening = True
        tray._on_chat_ipc_line(daemon.CHAT_IPC_PREFIX + json.dumps({"type": "confirmed", "data": "done"}))
        assert tray.chat_window is None

    def test_a_confirmed_event_shows_the_outcome_in_the_chat(self, qapp):
        from desktop_app.chat_window import ChatWindow
        win = ChatWindow(submit_fn=lambda text: None)
        try:
            line = daemon.CHAT_IPC_PREFIX + json.dumps({"type": "confirmed", "data": "Uninstalled Example App."})
            assert win.process_ipc_line(line) is True
            qapp.processEvents()
            assert [m["text"] for m in win._messages if m["kind"] == "assistant"] == ["Uninstalled Example App."]
        finally:
            win.close()
