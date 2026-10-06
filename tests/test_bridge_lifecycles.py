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
