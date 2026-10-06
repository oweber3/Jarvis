"""Each bridge's service builder, run through the reply-mode controller: nothing starts until a request
or warm-up needs it, runtime directories stay empty, and stopping closes only owned processes."""
from __future__ import annotations

import pytest

from claude_bridge_fakes import SIGNED_IN, FakeClaude, scripted
from claude_bridge_fakes import make_cfg as make_claude_cfg
from codex_bridge_fakes import FakeAppServer
from codex_bridge_fakes import make_cfg as make_codex_cfg
from jarvis.bridge import modes, runtime


@pytest.fixture(autouse=True)
def clean():
    modes.reset()
    yield
    modes.reset()


@pytest.mark.unit
class TestCodex:
    def test_building_starts_no_process_and_warm_up_checks_codex(self, tmp_path):
        from jarvis.codex_bridge import lifecycle

        built = []

        def client(cfg, workdir):
            server = FakeAppServer()
            built.append((server, workdir))
            return server

        cfg = make_codex_cfg(reply_mode="codex", codex_enabled=True, claude_enabled=False)
        service = lifecycle.create_service(cfg, client_factory=client, workdir=tmp_path / "rt")
        server, workdir = built[0]
        assert server.starts == 0 and service.mode == "codex"
        assert workdir == tmp_path / "rt" and workdir.is_dir()
        assert service.prepare() is None and server.starts == 1 and "account/read" in server.methods()
        assert list(workdir.iterdir()) == []
        service.close()

    def test_stopping_cancels_requests_and_closes_only_the_owned_process(self, tmp_path):
        from jarvis.codex_bridge import lifecycle

        servers = []

        def factory(cfg):
            def client(c, workdir):
                servers.append(FakeAppServer())
                return servers[-1]
            return lifecycle.create_service(cfg, client_factory=client, workdir=tmp_path)

        cfg = make_codex_cfg(reply_mode="codex", codex_enabled=True, claude_enabled=False)
        modes.start(cfg, factories={"codex": factory}, save=lambda v: True)
        assert modes.wait_for_warm_up(5)
        service = runtime.get_service()
        req = service.broker.submit_request("hi", [], "voice", "en", {})
        modes.stop()
        assert service.broker.state_of(req.id).value == "cancelled"
        assert servers[0].closes == 1 and runtime.get_service() is None


@pytest.mark.unit
class TestSetupSignInCheck:
    """The setup wizard's check: only the sign-in half of each preflight. No session, thread or turn
    starts and nothing about a request is sent; a Codex child started for it is stopped again."""

    def test_codex_signed_in_with_chatgpt_is_ready(self, tmp_path):
        from jarvis.codex_bridge import lifecycle

        server = FakeAppServer()
        assert lifecycle.check_sign_in(make_codex_cfg(), client_factory=lambda c, w: server,
                                       workdir=tmp_path) is None
        assert server.methods() == ["account/read"]
        assert server.closes == 1 and not server.alive

    @pytest.mark.parametrize("account,expected", [(None, "signed_out"), ({"type": "apiKey"}, "api_key_auth")])
    def test_codex_without_a_chatgpt_sign_in(self, tmp_path, account, expected):
        from jarvis.codex_bridge import lifecycle

        server = FakeAppServer(account=account)
        assert lifecycle.check_sign_in(make_codex_cfg(), client_factory=lambda c, w: server,
                                       workdir=tmp_path) == expected
        assert server.closes == 1

    @pytest.mark.parametrize("server,expected", [
        (lambda: FakeAppServer(start_error="not_found"), "not_found"),
        (lambda: FakeAppServer(start_error="timeout"), "start_failed"),
        (lambda: FakeAppServer(fail={"account/read": "unknown method"}), "unsupported"),
    ])
    def test_codex_that_cannot_be_checked_says_why(self, tmp_path, server, expected):
        from jarvis.codex_bridge import lifecycle

        fake = server()
        assert lifecycle.check_sign_in(make_codex_cfg(), client_factory=lambda c, w: fake,
                                       workdir=tmp_path) == expected
        assert "thread/start" not in fake.methods() and "turn/start" not in fake.methods()

    @pytest.mark.parametrize("auth,expected", [
        (dict(SIGNED_IN), None),
        (dict(SIGNED_IN, loggedIn=False), "signed_out"),
        (dict(SIGNED_IN, authMethod="api_key"), "api_key_auth"),
    ])
    def test_claude_sign_in(self, auth, expected):
        from jarvis.claude_bridge import lifecycle

        assert lifecycle.check_sign_in(make_claude_cfg(), auth_reader=lambda: auth) == expected

    @pytest.mark.parametrize("reason,expected", [("not_found", "not_found"), ("timeout", "start_failed")])
    def test_claude_that_cannot_be_checked_says_why(self, reason, expected):
        from jarvis.claude_bridge import lifecycle
        from jarvis.claude_bridge.cli import ClaudeCliError

        def fail():
            raise ClaudeCliError(reason)

        assert lifecycle.check_sign_in(make_claude_cfg(), auth_reader=fail) == expected


@pytest.mark.unit
class TestClaude:
    def test_building_starts_no_process_and_warm_up_checks_sign_in_and_model(self, tmp_path):
        from jarvis.claude_bridge import lifecycle

        claude = FakeClaude(scripted(("answer", "completed", "ok")))
        cfg = make_claude_cfg(reply_mode="claude", claude_enabled=True, codex_enabled=False)
        service = lifecycle.create_service(cfg, workdir=tmp_path / "rt", session_factory=claude,
                                           auth_reader=lambda: dict(SIGNED_IN))
        assert claude.sessions == [] and service.mode == "claude"
        assert service.prepare() is None
        assert claude.sessions and (tmp_path / "rt").is_dir() and list((tmp_path / "rt").iterdir()) == []
        service.close()
        assert all(not s.alive for s in claude.sessions)

    def test_switching_from_claude_to_local_stops_every_claude_process(self, tmp_path):
        from jarvis.claude_bridge import lifecycle

        claude = FakeClaude(scripted(("answer", "completed", "ok")))
        cfg = make_claude_cfg(reply_mode="claude", claude_enabled=True, codex_enabled=False)
        modes.start(cfg, save=lambda v: True, factories={"claude": lambda c: lifecycle.create_service(
            c, workdir=tmp_path, session_factory=claude, auth_reader=lambda: dict(SIGNED_IN))})
        assert modes.wait_for_warm_up(5)
        outcome = runtime.get_service().run_request("hello", [], "voice", "en", None, False)
        assert outcome.kind == "reply"
        assert modes.switch("local").ok
        assert claude.sessions and all(not s.alive for s in claude.sessions)
