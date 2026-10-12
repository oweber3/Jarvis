"""Stop, spare sessions and closing for both background bridges (fake Claude Code sessions and a fake
Codex app-server: no real process, no network).

See ``bridge/bridge.spec.md`` (Request lifecycle, Reply path, Cloud models, Reply modes and switching).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from claude_bridge_fakes import SIGNED_IN, FakeClaude, FakeSession, make_cfg, scripted
from codex_bridge_fakes import LUNA, FakeAppServer, FakeExecutor, FakeStore
from codex_bridge_fakes import make_cfg as codex_cfg
from codex_bridge_fakes import scripted as codex_scripted
from jarvis.bridge.broker import State
from jarvis.claude_bridge.service import ClaudeBridgeService
from jarvis.codex_bridge.service import BridgeService

TOOLS = {
    "getTime": {"description": "time", "inputSchema": {"type": "object", "properties": {}, "required": []}},
}


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def build_claude(script=None, cfg=None, auth_reader=None, factory=None):
    claude = factory or FakeClaude(script)
    service = ClaudeBridgeService(cfg or make_cfg(), claude, executor=FakeExecutor(), confirmation_store=FakeStore(),
                                  tools_provider=lambda c: dict(TOOLS),
                                  auth_reader=auth_reader or (lambda: dict(SIGNED_IN)),
                                  instructions_provider=lambda: "CONTRACT")
    return service, claude


def build_codex(server, cfg=None):
    return BridgeService(cfg or codex_cfg(), server, executor=FakeExecutor(), confirmation_store=FakeStore(),
                         tools_provider=lambda c: dict(TOOLS), runtime_dir=Path("."))


def run(service, text="open word"):
    return service.run_request(text, context=[], origin="voice", language="en", db=None, quiet=False)


def run_in_background(service, text="open word"):
    holder = {}
    worker = threading.Thread(target=lambda: holder.setdefault("out", run(service, text)), daemon=True)
    worker.start()
    return worker, holder


def queued(service):
    return any(r.state is State.QUEUED for r in service.broker._requests.values())


@pytest.mark.unit
class TestStopWhileQueuedBehindTheReadinessCheck:
    """A request that arrives while the readiness check (``prepare``, at start-up or after a model change)
    holds the turn is queued; a stop cancels it (``queued -> cancelled``) and nothing is sent."""

    def test_claude(self):
        gate = threading.Event()

        def slow_auth():
            gate.wait(5.0)
            return dict(SIGNED_IN)

        service, claude = build_claude(scripted(("answer", "completed", "Opened Word.")), auth_reader=slow_auth)
        warm = threading.Thread(target=service.prepare, daemon=True)
        warm.start()
        assert wait_for(lambda: service._turn_lock.locked())
        worker, holder = run_in_background(service)
        try:
            assert wait_for(lambda: queued(service))
            assert service.is_busy(), "a queued request counts as in flight, so a spoken stop reaches it"
            assert service.cancel_active("stop") is True
        finally:
            gate.set()
            worker.join(10.0)
            warm.join(10.0)
            service.close()

        assert holder["out"].kind == "cancelled"
        assert claude.used() == []
        assert not service.is_busy()

    def test_codex(self):
        gate = threading.Event()

        class SlowPreflight(FakeAppServer):
            def request(self, method, params, timeout_sec):
                if method == "account/read":
                    gate.wait(5.0)
                return super().request(method, params, timeout_sec)

        server = SlowPreflight(codex_scripted(("answer", "completed", "Opened Word.")))
        service = build_codex(server)
        warm = threading.Thread(target=service.prepare, daemon=True)
        warm.start()
        assert wait_for(lambda: service._turn_lock.locked())
        worker, holder = run_in_background(service)
        try:
            assert wait_for(lambda: queued(service))
            assert service.is_busy()
            assert service.cancel_active("stop") is True
        finally:
            gate.set()
            worker.join(10.0)
            warm.join(10.0)
            service.close()

        assert holder["out"].kind == "cancelled"
        assert server.turns == []
        assert not service.is_busy()

    def test_with_nothing_running_or_waiting_stop_reports_nothing_cancelled(self):
        service, _ = build_claude(scripted(("answer", "completed", "Done.")))
        try:
            assert service.is_busy() is False
            assert service.cancel_active("stop") is False
        finally:
            service.close()


class _SlowStartClaude(FakeClaude):
    """Sessions created while ``delay_next_sec`` is set take that long to finish their handshake."""

    def __init__(self, script=None):
        super().__init__(script)
        self.delay_next_sec = 0.0

    def __call__(self, session_id, args):
        session = _SlowSession(self, session_id, args, self.delay_next_sec)
        self.delay_next_sec = 0.0
        self.sessions.append(session)
        return session


class _SlowSession(FakeSession):
    def __init__(self, factory, session_id, args, delay):
        super().__init__(factory, session_id, args)
        self._delay = delay

    def _initialise(self, request_id):
        if self._delay:
            time.sleep(self._delay)
        super()._initialise(request_id)


@pytest.mark.unit
class TestSpareStartedWithOldSettings:
    """A spare session or thread started with the old model or effort is closed and never used."""

    def test_a_late_claude_spare_is_closed_and_the_next_request_uses_the_new_model(self):
        claude = _SlowStartClaude(scripted(("answer", "completed", "Done.")))
        service, _ = build_claude(cfg=make_cfg(claude_model="sonnet"), factory=claude)
        try:
            original_prestart = service._prestart

            def slow_prestart():
                claude.delay_next_sec = 3.0  # longer than reconfigure waits for a starting spare
                original_prestart()

            service._prestart = slow_prestart
            assert run(service, "hello").kind == "reply"
            service._prestart = original_prestart
            late_spare = claude.sessions[-1]

            assert service.reconfigure(make_cfg(claude_model="haiku"))
            assert run(service, "hello again").kind == "reply"

            used = claude.used()[-1]
            assert used.arg("--model") == "haiku"
            assert late_spare.arg("--model") == "sonnet"
            assert late_spare.user_texts == [] and late_spare.closes >= 1
        finally:
            service.close()

    def test_a_codex_spare_finishing_after_a_model_change_is_closed_and_never_used(self):
        gate = threading.Event()
        holding = {"on": False}

        class SlowSpare(FakeAppServer):
            def request(self, method, params, timeout_sec):
                if method == "thread/start" and holding["on"]:
                    gate.wait(5.0)
                return super().request(method, params, timeout_sec)

        other = {"id": "gpt-6-sol", "model": "gpt-6-sol",
                 "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}
        server = SlowSpare(codex_scripted(("answer", "completed", "Done.")), models=[LUNA, other])
        service = build_codex(server)
        try:
            assert service.prepare() is None
            assert wait_for(lambda: service._spare_idle.is_set())
            holding["on"] = True
            assert run(service, "hello").kind == "reply"  # its end starts a spare whose thread/start waits
            assert wait_for(lambda: not service._spare_idle.is_set())

            switched = threading.Thread(
                target=lambda: service.reconfigure(codex_cfg(codex_model="gpt-6-sol")), daemon=True)
            switched.start()
            time.sleep(0.2)
            holding["on"] = False
            gate.set()
            switched.join(10.0)
            late_thread = list(server.threads)[-1]
            assert server.threads[late_thread]["model"] == "gpt-6-luna"
            assert late_thread in server.unsubscribed

            assert run(service, "hello again").kind == "reply"
            last_turn = server.params_of("turn/start")[-1]
            assert last_turn["threadId"] != late_thread
            assert server.threads[last_turn["threadId"]]["model"] == "gpt-6-sol"
        finally:
            gate.set()
            service.close()


@pytest.mark.unit
class TestNoProcessAfterClose:
    """A readiness check waiting for a running request when the user switches away starts nothing once
    the turn is free: a closed service starts no process."""

    def test_codex_does_not_restart_its_child(self):
        server = FakeAppServer(codex_scripted(("answer", "completed", "Done.")))
        service = build_codex(server)
        assert service.prepare() is None
        assert server.alive and server.starts == 1

        service._turn_lock.acquire()  # a request is running
        recheck = threading.Thread(target=service.prepare, daemon=True)
        recheck.start()
        time.sleep(0.2)
        assert recheck.is_alive()

        service.close()
        service._turn_lock.release()
        recheck.join(5.0)
        wait_for(lambda: service._spare_idle.is_set(), 2.0)

        assert not server.alive
        assert server.starts == 1

    def test_claude_starts_no_session(self):
        service, claude = build_claude(scripted(("answer", "completed", "Done.")))
        service._turn_lock.acquire()  # a request is running
        recheck = threading.Thread(target=service.prepare, daemon=True)
        recheck.start()
        time.sleep(0.2)
        assert recheck.is_alive()

        service.close()
        service._turn_lock.release()
        recheck.join(5.0)

        assert claude.sessions == []
