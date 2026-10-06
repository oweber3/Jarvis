"""Behavioural tests for the Claude Code CLI client (subprocess fixture, no real Claude)."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis.claude_bridge.cli import (
    ClaudeCliError,
    ClaudeSession,
    child_environment,
    read_auth_status,
    resolve_executable,
    session_args,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_claude_cli.py"
SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}


def fixture_factory(mode="answer"):
    def factory(args, **kwargs):
        return subprocess.Popen([sys.executable, str(FIXTURE), mode, *args[1:]], **kwargs)
    return factory


def args_for(**kw):
    base = dict(model="haiku", effort=None, max_turns=10, system_prompt="CONTRACT", answer_schema=SCHEMA,
                session_id="11111111-2222-3333-4444-555555555555")
    base.update(kw)
    return session_args(**base)


def make(tmp_path, mode="answer", **kw):
    return ClaudeSession("claude", args_for(**kw), session_id="11111111-2222-3333-4444-555555555555",
                         cwd=tmp_path, process_factory=fixture_factory(mode))


def answer_handshake(session, until_kind="control_response", timeout=10.0):
    """Answer the in-process MCP handshake like the bridge does; return the first event of ``until_kind``."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        event = session.poll_event(0.2)
        if event is None:
            continue
        if event["kind"] == "control_request" and event["request"].get("subtype") == "mcp_message":
            message = event["request"]["message"]
            if message.get("id") is None:
                session.respond(event["request_id"], {"mcp_response": {"jsonrpc": "2.0", "id": 0, "result": {}}})
            else:
                result = {"tools": []} if message["method"] == "tools/list" else {"capabilities": {"tools": {}}}
                session.respond(event["request_id"], {"mcp_response": {"jsonrpc": "2.0", "id": message["id"],
                                                                       "result": result}})
            continue
        if event["kind"] == until_kind:
            return event
    pytest.fail(f"no {until_kind} event")


@pytest.mark.unit
class TestChildEnvironment:
    def test_child_gets_analytics_off_and_no_inherited_claude_or_anthropic_variables(self, tmp_path, monkeypatch):
        record = tmp_path / "record.json"
        monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.invalid")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "parent-session")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
        session = make(tmp_path)
        session.start()
        try:
            end = time.monotonic() + 10
            while not record.exists() and time.monotonic() < end:
                time.sleep(0.05)
            time.sleep(0.1)
            env = json.loads(record.read_text())["env"]
        finally:
            session.close()
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert env["DISABLE_TELEMETRY"] == "1"
        assert env["DISABLE_ERROR_REPORTING"] == "1" and env["DISABLE_AUTOUPDATER"] == "1"
        assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
        assert env["ANTHROPIC_API_KEY"] is None and env["ANTHROPIC_BASE_URL"] is None
        assert env["CLAUDECODE"] is None and env["CLAUDE_CODE_ENTRYPOINT"] is None
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-home")

    def test_jarvis_own_environment_is_not_changed(self, monkeypatch):
        import os
        monkeypatch.setenv("ANTHROPIC_API_KEY", "keep-me")
        monkeypatch.delenv("DISABLE_TELEMETRY", raising=False)
        child = child_environment(os.environ)
        assert "ANTHROPIC_API_KEY" not in child and child["DISABLE_TELEMETRY"] == "1"
        assert os.environ["ANTHROPIC_API_KEY"] == "keep-me" and "DISABLE_TELEMETRY" not in os.environ


@pytest.mark.unit
class TestSessionArguments:
    def test_isolation_flags(self):
        args = args_for()
        joined = " ".join(args)
        assert args[:6] == ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        assert args[args.index("--tools") + 1] == ""
        assert args[args.index("--setting-sources") + 1] == ""
        assert "--strict-mcp-config" in args and "--no-session-persistence" in args
        assert "--disable-slash-commands" in args
        assert args[args.index("--permission-mode") + 1] == "dontAsk"
        assert args[args.index("--allowedTools") + 1] == "mcp__jarvis__jarvis_execute"
        assert json.loads(args[args.index("--mcp-config") + 1]) == {
            "mcpServers": {"jarvis": {"type": "sdk", "name": "jarvis"}}}
        assert json.loads(args[args.index("--settings") + 1]) == {"crossSessionInbound": "refuse"}
        assert args[args.index("--system-prompt") + 1] == "CONTRACT"
        assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA
        assert args[args.index("--session-id") + 1] == "11111111-2222-3333-4444-555555555555"
        assert "--dangerously-skip-permissions" not in joined and "--resume" not in joined

    def test_model_effort_and_turn_bound(self):
        args = args_for(model="sonnet", effort="low", max_turns=6)
        assert args[args.index("--model") + 1] == "sonnet"
        assert args[args.index("--effort") + 1] == "low"
        assert args[args.index("--max-turns") + 1] == "6"
        assert "--effort" not in args_for(effort=None)


@pytest.mark.unit
class TestProtocol:
    def test_handshake_then_turn_and_result(self, tmp_path):
        session = make(tmp_path, "answer")
        session.start()
        try:
            init_id = session.send_control("initialize")
            response = answer_handshake(session)
            assert response["request_id"] == init_id and response["response"]["subtype"] == "success"
            assert [m["value"] for m in response["response"]["response"]["models"]][:2] == ["default", "sonnet"]
            session.send_user_text('Jarvis request {"request_id": "r1", "utterance": "hello"}')
            result = None
            end = time.monotonic() + 10
            while result is None and time.monotonic() < end:
                event = session.poll_event(0.2)
                if event and event["kind"] == "message" and event["type"] == "result":
                    result = event["message"]
            assert result["structured_output"] == {"status": "completed", "reply": "You said: hello"}
        finally:
            session.close()
        assert not session.alive

    @pytest.mark.parametrize("mode, reason", [("oversized", "oversized"), ("malformed", "malformed"),
                                              ("exit_now", "exited")])
    def test_bad_output_or_exit_closes_the_session(self, tmp_path, mode, reason):
        session = ClaudeSession("claude", args_for(), session_id="s", cwd=tmp_path,
                                process_factory=fixture_factory(mode), max_message_bytes=1024 * 1024)
        session.start()
        try:
            event = None
            end = time.monotonic() + 10
            while time.monotonic() < end:
                event = session.poll_event(0.2)
                if event and event["kind"] == "closed":
                    break
            assert event["kind"] == "closed" and event["reason"] == reason
            assert not session.alive
            with pytest.raises(ClaudeCliError) as err:
                session.send_control("initialize")
            assert err.value.reason == "closed"
        finally:
            session.close()

    def test_missing_executable_is_reported(self, tmp_path):
        def missing(args, **kwargs):
            raise FileNotFoundError(args[0])

        session = ClaudeSession("claude", args_for(), session_id="s", cwd=tmp_path, process_factory=missing)
        with pytest.raises(ClaudeCliError) as err:
            session.start()
        assert err.value.reason == "not_found"

    def test_close_is_bounded_and_repeatable(self, tmp_path):
        session = make(tmp_path, "no_init")
        session.start()
        started = time.monotonic()
        session.close(timeout_sec=1.0)
        session.close()
        assert time.monotonic() - started < 8 and not session.alive


@pytest.mark.unit
class TestExecutableAndSignIn:
    def test_explicit_paths_are_used_as_given(self, tmp_path):
        exe = tmp_path / "claude.exe"
        exe.write_text("")
        assert resolve_executable(str(exe)) == str(exe)
        assert resolve_executable(str(tmp_path / "missing.exe")) is None

    def test_default_name_falls_back_to_the_native_installer_location(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert resolve_executable("claude") is None
        installed = tmp_path / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
        installed.parent.mkdir(parents=True)
        installed.write_text("")
        assert resolve_executable("claude") == str(installed)
        assert resolve_executable("other-tool") is None

    @pytest.mark.parametrize("method", ["claude.ai", "none", "api_key"])
    def test_auth_status_reports_sign_in_without_tokens(self, monkeypatch, method):
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", method)

        def runner(args, **kwargs):
            return subprocess.run([sys.executable, str(FIXTURE), "auth", *args[1:]], **kwargs)

        status = read_auth_status("claude", runner=runner)
        assert status["authMethod"] == method and status["loggedIn"] is (method != "none")
        assert set(status) == {"loggedIn", "authMethod", "apiProvider", "subscriptionType"}
