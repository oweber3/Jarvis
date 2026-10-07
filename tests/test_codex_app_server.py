"""Behavioural tests for the Codex app-server client (subprocess fixture, no real Codex)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis.codex_bridge.app_server import (
    ISOLATION_OVERRIDES,
    AppServerClient,
    AppServerError,
    resolve_executable,
    thread_isolation_config,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


def fixture_factory(mode="normal", record=None):
    """A process factory that runs the fake server, keeping the client's own arguments."""

    def factory(args, **kwargs):
        if record is not None:
            record.append((list(args), dict(kwargs)))
        kwargs.pop("creationflags", None)
        return subprocess.Popen([sys.executable, str(FIXTURE), mode, *args[1:]], **kwargs)

    return factory


def make(tmp_path, mode="normal", record=None, **kw):
    kw.setdefault("start_timeout_sec", 10.0)
    return AppServerClient("codex", cwd=tmp_path, process_factory=fixture_factory(mode, record), **kw)


def next_event(client, timeout=5.0):
    event = client.poll_event(timeout)
    assert event is not None, "expected an event"
    return event


@pytest.fixture
def client(tmp_path):
    c = make(tmp_path)
    yield c
    c.close()


@pytest.mark.unit
class TestHandshake:
    def test_initialize_precedes_initialized_and_opts_into_experimental_api(self, client):
        result = client.start()
        assert result["params"]["capabilities"]["experimentalApi"] is True
        assert client.request("test/order", {}, 5)["received"][:2] == ["initialize", "initialized"]

    def test_start_is_idempotent_while_running(self, client):
        client.start()
        generation = client.generation
        client.start()
        assert client.generation == generation

    def test_start_times_out_when_the_server_never_initialises(self, tmp_path):
        c = make(tmp_path, mode="no_init", start_timeout_sec=0.5)
        try:
            with pytest.raises(AppServerError) as err:
                c.start()
            assert err.value.reason == "timeout"
            assert not c.alive
        finally:
            c.close()

    def test_missing_executable_is_reported(self, tmp_path):
        def missing(args, **kwargs):
            raise FileNotFoundError(args[0])

        c = AppServerClient("codex", cwd=tmp_path, process_factory=missing)
        with pytest.raises(AppServerError) as err:
            c.start()
        assert err.value.reason == "not_found"


@pytest.mark.unit
class TestLaunch:
    def test_child_runs_hidden_without_a_shell_and_with_isolation_overrides(self, tmp_path):
        record = []
        c = make(tmp_path, record=record)
        try:
            c.start()
            args, kwargs = record[0]
        finally:
            c.close()
        assert args[:4] == ["codex", "app-server", "--listen", "stdio://"]
        overrides = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
        assert overrides == list(ISOLATION_OVERRIDES)
        assert "--analytics-default-enabled" not in args
        assert "analytics.enabled=false" in overrides
        assert not kwargs.get("shell")
        assert Path(kwargs["cwd"]) == tmp_path
        assert kwargs["env"]["RUST_LOG"] == "warn"
        if sys.platform == "win32":
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW

    def test_isolation_disables_the_tools_that_widen_the_surface(self):
        joined = " ".join(ISOLATION_OVERRIDES)
        for key in ("features.shell_tool=false", "features.apps=false",
                    "features.plugins=false", "features.computer_use=false", "features.multi_agent=false",
                    "web_search=\"disabled\"", "history.persistence=\"none\"", "notify=[]",
                    "project_doc_max_bytes=0"):
            assert key in joined

    def test_thread_config_disables_every_configured_mcp_server_and_plugin_by_name(self):
        effective = {"mcp_servers": {"alpha": {"command": "secret-path"}, "beta": {}},
                     "plugins": {"p@market": {"enabled": True}}, "model": "x"}
        cfg = thread_isolation_config(effective)
        assert cfg == {"mcp_servers.alpha.enabled": False, "mcp_servers.beta.enabled": False,
                       "plugins.p@market.enabled": False}
        assert thread_isolation_config({}) == {}
        assert thread_isolation_config({"mcp_servers": None}) == {}


@pytest.mark.unit
class TestMessages:
    def test_interleaved_fragmented_and_coalesced_messages_are_routed(self, client):
        client.start()
        assert client.request("test/events", {}, 5) == {"ok": True}
        note = next_event(client)
        assert (note["kind"], note["method"], note["params"]) == ("notification", "test/note", {"n": 1})
        req = next_event(client)
        assert (req["kind"], req["method"], req["id"]) == ("request", "item/tool/call", "srv-1")
        assert note["generation"] == req["generation"] == client.generation

    def test_responses_to_server_requests_reach_the_server(self, client):
        client.start()
        client.request("test/events", {}, 5)
        next_event(client)
        req = next_event(client)
        client.respond(req["id"], {"success": True})
        echoed = next_event(client)
        assert echoed["method"] == "test/responded"
        assert echoed["params"]["id"] == "srv-1" and echoed["params"]["result"] == {"success": True}
        client.respond_error("srv-2", -32601, "not supported")
        echoed = next_event(client)
        assert echoed["params"]["error"]["code"] == -32601

    def test_error_responses_raise_with_a_bounded_message(self, client):
        client.start()
        with pytest.raises(AppServerError) as err:
            client.request("test/error", {}, 5)
        assert err.value.reason == "rpc_error" and err.value.code == -32600
        assert len(str(err.value.message)) <= 400

    def test_a_missing_response_times_out_and_the_client_stays_usable(self, client):
        client.start()
        with pytest.raises(AppServerError) as err:
            client.request("test/slow", {}, 0.3)
        assert err.value.reason == "timeout"
        assert client.request("test/order", {}, 5)["received"]

    def test_poll_returns_none_when_idle(self, client):
        client.start()
        assert client.poll_event(0.05) is None

    def test_heavy_stderr_does_not_block_the_protocol(self, tmp_path):
        c = make(tmp_path, mode="stderr_flood")
        try:
            c.start()
            assert c.request("test/order", {}, 5)["received"]
        finally:
            c.close()


@pytest.mark.unit
class TestFailures:
    def test_oversized_message_closes_the_generation(self, client):
        client.start()
        with pytest.raises(AppServerError) as err:
            client.request("test/big", {}, 10)
        assert err.value.reason == "closed"
        event = next_event(client)
        assert (event["kind"], event["reason"]) == ("closed", "oversized")
        assert not client.alive

    def test_malformed_output_closes_the_generation(self, client):
        client.start()
        with pytest.raises(AppServerError):
            client.request("test/bad", {}, 5)
        event = next_event(client)
        assert (event["kind"], event["reason"]) == ("closed", "malformed")

    def test_unexpected_exit_fails_pending_requests_and_a_restart_is_a_new_generation(self, client):
        client.start()
        first = client.generation
        with pytest.raises(AppServerError) as err:
            client.request("test/exit", {}, 5)
        assert err.value.reason == "closed"
        event = next_event(client)
        assert (event["kind"], event["reason"], event["generation"]) == ("closed", "exited", first)
        assert not client.alive
        with pytest.raises(AppServerError) as err:
            client.request("test/order", {}, 1)
        assert err.value.reason == "closed"
        client.start()
        assert client.generation == first + 1
        assert client.request("test/order", {}, 5)["received"]

    def test_close_is_bounded_even_when_the_child_ignores_end_of_input(self, tmp_path):
        c = make(tmp_path, mode="ignore_eof")
        c.start()
        began = time.monotonic()
        c.close(timeout_sec=0.5)
        assert time.monotonic() - began < 5
        assert not c.alive
        c.close()


@pytest.mark.unit
class TestExecutableResolution:
    def test_explicit_existing_path_is_used(self, tmp_path):
        exe = tmp_path / "codex.exe"
        exe.write_bytes(b"")
        assert resolve_executable(str(exe)) == str(exe)

    def test_explicit_missing_path_is_not_found(self, tmp_path):
        assert resolve_executable(str(tmp_path / "nope" / "codex.exe")) is None

    def test_bare_name_is_found_on_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda name: str(tmp_path / "found.exe") if name == "codex" else None)
        assert resolve_executable("codex") == str(tmp_path / "found.exe")

    def test_default_name_falls_back_to_the_desktop_app_copy(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        old = tmp_path / "OpenAI" / "Codex" / "bin" / "aaa" / "codex.exe"
        new = tmp_path / "OpenAI" / "Codex" / "bin" / "bbb" / "codex.exe"
        for exe in (old, new):
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
        os.utime(old, (1, 1))
        assert resolve_executable("codex") == str(new)

    def test_other_bare_names_do_not_fall_back(self, monkeypatch, tmp_path):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        exe = tmp_path / "OpenAI" / "Codex" / "bin" / "aaa" / "codex.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        assert resolve_executable("other-codex") is None


def _mac(monkeypatch, tmp_path):
    """A macOS layout under tmp_path: filesystem root and home folder, nothing on PATH."""
    root, home = tmp_path / "root", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("jarvis.codex_bridge.app_server.sys.platform", "darwin")
    monkeypatch.setattr("jarvis.codex_bridge.app_server._FS_ROOT", root)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return root, home


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


@pytest.mark.unit
class TestMacExecutableResolution:
    """A Mac app started from the Dock gets a short PATH without Homebrew or the user's bin folders."""

    def test_homebrew_install_is_found_without_path(self, monkeypatch, tmp_path):
        root, _ = _mac(monkeypatch, tmp_path)
        exe = _touch(root / "opt" / "homebrew" / "bin" / "codex")
        assert resolve_executable("codex") == str(exe)

    def test_intel_homebrew_install_is_found_without_path(self, monkeypatch, tmp_path):
        root, _ = _mac(monkeypatch, tmp_path)
        exe = _touch(root / "usr" / "local" / "bin" / "codex")
        assert resolve_executable("codex") == str(exe)

    def test_user_bin_install_is_found_without_path(self, monkeypatch, tmp_path):
        _, home = _mac(monkeypatch, tmp_path)
        exe = _touch(home / ".local" / "bin" / "codex")
        assert resolve_executable("codex") == str(exe)

    @pytest.mark.parametrize("app", ["ChatGPT.app", "Codex.app"])
    def test_desktop_app_copy_is_found(self, monkeypatch, tmp_path, app):
        root, _ = _mac(monkeypatch, tmp_path)
        exe = _touch(root / "Applications" / app / "Contents" / "Resources" / "codex")
        assert resolve_executable("codex") == str(exe)

    def test_desktop_app_in_user_applications_is_found(self, monkeypatch, tmp_path):
        _, home = _mac(monkeypatch, tmp_path)
        exe = _touch(home / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex")
        assert resolve_executable("codex") == str(exe)

    def test_desktop_app_copy_in_a_resources_subfolder_is_found(self, monkeypatch, tmp_path):
        root, _ = _mac(monkeypatch, tmp_path)
        exe = _touch(root / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "bin" / "codex")
        assert resolve_executable("codex") == str(exe)

    def test_chatgpt_app_codex_cli_bundle_is_found(self, monkeypatch, tmp_path):
        """The layout a real ChatGPT.app ships: a ``codex-cli`` folder with ``bin/codex`` and a nested app."""
        root, _ = _mac(monkeypatch, tmp_path)
        cli = root / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex-cli"
        _touch(cli / "CodexCLI.app" / "Contents" / "MacOS" / "codex")
        exe = _touch(cli / "bin" / "codex")
        assert resolve_executable("codex") == str(exe)

    def test_standalone_install_wins_over_the_desktop_app_copy(self, monkeypatch, tmp_path):
        root, _ = _mac(monkeypatch, tmp_path)
        _touch(root / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex")
        brew = _touch(root / "opt" / "homebrew" / "bin" / "codex")
        assert resolve_executable("codex") == str(brew)

    def test_a_folder_named_codex_is_not_an_executable(self, monkeypatch, tmp_path):
        root, _ = _mac(monkeypatch, tmp_path)
        (root / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex").mkdir(parents=True)
        assert resolve_executable("codex") is None

    def test_nothing_installed_is_not_found(self, monkeypatch, tmp_path):
        _mac(monkeypatch, tmp_path)
        assert resolve_executable("codex") is None

    def test_other_bare_names_do_not_fall_back(self, monkeypatch, tmp_path):
        root, _ = _mac(monkeypatch, tmp_path)
        _touch(root / "opt" / "homebrew" / "bin" / "codex")
        assert resolve_executable("other-codex") is None

    def test_path_still_wins(self, monkeypatch, tmp_path):
        root, _ = _mac(monkeypatch, tmp_path)
        _touch(root / "opt" / "homebrew" / "bin" / "codex")
        monkeypatch.setattr("shutil.which", lambda name: "/elsewhere/codex" if name == "codex" else None)
        assert resolve_executable("codex") == "/elsewhere/codex"


LIVE = os.environ.get("JARVIS_CODEX_LIVE") == "1"


@pytest.mark.integration
@pytest.mark.skipif(not LIVE, reason="live Codex runtime check (set JARVIS_CODEX_LIVE=1)")
class TestLiveRuntime:
    """No-inference checks against the installed Codex runtime."""

    def test_isolated_handshake_and_ephemeral_thread(self, tmp_path):
        exe = resolve_executable("codex")
        assert exe, "Codex executable not found"
        c = AppServerClient(exe, cwd=tmp_path)
        try:
            c.start()
            features = {f["name"]: f["enabled"]
                        for f in c.request("experimentalFeature/list", {"limit": 500}, 20)["data"]}
            for name in ("shell_tool", "apps", "plugins", "computer_use", "multi_agent",
                         "image_generation", "browser_use", "hooks", "memories"):
                assert features.get(name) is False, name
            effective = c.request("config/read", {"cwd": str(tmp_path)}, 20)["config"]
            assert effective["history"]["persistence"] == "none"
            assert effective.get("notify") in ([], None)
            started = c.request("thread/start", {
                "ephemeral": True, "cwd": str(tmp_path), "sandbox": "read-only", "approvalPolicy": "never",
                "environments": [], "baseInstructions": "probe", "config": thread_isolation_config(effective),
                "dynamicTools": [{"type": "function", "name": "probe_tool", "description": "probe",
                                  "inputSchema": {"type": "object", "properties": {}}}],
            }, 30)
            thread = started["thread"]
            assert thread["ephemeral"] is True and thread.get("path") is None
            assert started["instructionSources"] == []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                event = c.poll_event(0.2)
                if event and event.get("method") == "mcpServer/startupStatus/updated":
                    pytest.fail("an MCP server started despite the isolation overrides")
            c.request("thread/unsubscribe", {"threadId": thread["id"]}, 10)
        finally:
            c.close()
