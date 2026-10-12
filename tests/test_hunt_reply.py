"""Bug-hunt repro tests for the reply engine and the background reply bridges.

Each test states the behaviour the specs promise and drives the real service code with the in-memory
fakes (no real Claude Code or Codex process, no network, no Ollama).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from claude_bridge_fakes import SIGNED_IN, FakeClaude, FakeSession, make_cfg, scripted
from codex_bridge_fakes import FakeAppServer, FakeExecutor, FakeStore
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


def run_in_background(service, text="open word"):
    holder = {}

    def go():
        holder["out"] = service.run_request(text, context=[], origin="voice", language="en", db=None,
                                            quiet=False)

    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    return worker, holder


@pytest.mark.unit
class TestStopWhileQueuedBehindWarmUp:
    """bridge.spec.md: "A stop command or the chat Stop button cancels the active request" and the
    lifecycle allows ``queued -> cancelled`` on a user stop. A request that arrives while the mode's
    start-up check (``prepare``) still holds the turn lock is queued, yet Stop cannot reach it."""

    def test_stop_cancels_a_request_queued_behind_the_start_up_check(self):
        gate = threading.Event()

        def slow_auth():
            gate.wait(5.0)  # the start-up check is still running (auth status, probe session)
            return dict(SIGNED_IN)

        service, claude = build_claude(scripted(("answer", "completed", "Opened Word.")), auth_reader=slow_auth)
        warm = threading.Thread(target=service.prepare, daemon=True)
        warm.start()
        assert wait_for(lambda: service._turn_lock.locked())

        worker, holder = run_in_background(service)
        assert wait_for(lambda: any(r.state is State.QUEUED for r in service.broker._requests.values()))

        # What the listener checks before treating "stop" as a stop, and what Stop calls.
        busy = service.is_busy()
        cancelled = service.cancel_active("stop")

        gate.set()
        worker.join(10.0)
        warm.join(10.0)
        service.close()

        problems = []
        if not busy:
            problems.append("is_busy() is False, so runtime.request_active() ignores a spoken stop")
        if not cancelled:
            problems.append("cancel_active('stop') returned False")
        if holder["out"].kind != "cancelled":
            problems.append(f"outcome was {holder['out'].kind!r}")
        if claude.used():
            problems.append("the stopped request was still sent to Claude")
        assert not problems, "; ".join(problems)


    def test_stop_cancels_a_codex_request_queued_behind_the_start_up_check(self):
        gate = threading.Event()

        class SlowPreflight(FakeAppServer):
            def request(self, method, params, timeout_sec):
                if method == "account/read":
                    gate.wait(5.0)
                return super().request(method, params, timeout_sec)

        server = SlowPreflight(codex_scripted(("answer", "completed", "Opened Word.")))
        service = BridgeService(codex_cfg(), server, executor=FakeExecutor(), confirmation_store=FakeStore(),
                                tools_provider=lambda c: dict(TOOLS), runtime_dir=Path("."))
        warm = threading.Thread(target=service.prepare, daemon=True)
        warm.start()
        assert wait_for(lambda: service._turn_lock.locked())

        worker, holder = run_in_background(service)
        assert wait_for(lambda: any(r.state is State.QUEUED for r in service.broker._requests.values()))
        busy = service.is_busy()
        cancelled = service.cancel_active("stop")

        gate.set()
        worker.join(10.0)
        warm.join(10.0)
        service.close()

        problems = []
        if not busy:
            problems.append("is_busy() is False, so runtime.request_active() ignores a spoken stop")
        if not cancelled:
            problems.append("cancel_active('stop') returned False")
        if holder["out"].kind != "cancelled":
            problems.append(f"outcome was {holder['out'].kind!r}")
        if server.turns:
            problems.append("the stopped request was still sent to Codex")
        assert not problems, "; ".join(problems)


class _SlowStartClaude(FakeClaude):
    """A session factory whose sessions can be made slow to finish the start-up handshake."""

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
class TestSpareSessionAfterModelChange:
    """bridge.spec.md, Cloud models: after ``reconfigure`` "a spare session or thread started with the
    old model is closed and never used". The Claude service waits only 2 s for a spare that is still
    starting, so a spare that finishes its handshake later is kept and carries the next request on
    the old model."""

    def test_next_request_runs_on_the_new_model_not_a_late_spare(self):
        claude = _SlowStartClaude(scripted(("answer", "completed", "Done.")))
        service, _ = build_claude(cfg=make_cfg(claude_model="sonnet"), factory=claude)
        try:
            # First request; its end starts the spare session for the next request, which is slow.
            claude.delay_next_sec = 0.0
            original_prestart = service._prestart

            def slow_prestart():
                claude.delay_next_sec = 3.0
                original_prestart()

            service._prestart = slow_prestart
            first = service.run_request("hello", context=[], origin="voice", language="en", db=None, quiet=False)
            assert first.kind == "reply"
            service._prestart = original_prestart

            # The user picks another model while the spare is still starting.
            assert service.reconfigure(make_cfg(claude_model="haiku"))

            second = service.run_request("hello again", context=[], origin="voice", language="en", db=None,
                                         quiet=False)
            assert second.kind == "reply"
            used = claude.used()[-1]
            assert used.arg("--model") == "haiku", (
                f"the request after the model change ran on {used.arg('--model')!r}")
        finally:
            service.close()


@pytest.mark.unit
class TestNoProcessAfterClose:
    """bridge.spec.md, Reply modes and switching: switching "stops the previous bridge's processes" and "a
    closed service refuses later requests and warm-up as cancelled ... and starts no process". A Codex
    readiness check (``prepare``, run by ``modes.recheck_active`` after a model change) that is waiting for
    the turn lock when the user switches away checks ``_closed`` only before it waits, so once the lock is
    free it starts a fresh ``codex app-server`` (and a spare thread) for a service nobody will ever close."""

    def test_a_waiting_codex_readiness_check_does_not_restart_the_child_after_close(self):
        server = FakeAppServer(codex_scripted(("answer", "completed", "Done.")))
        service = BridgeService(codex_cfg(), server, executor=FakeExecutor(), confirmation_store=FakeStore(),
                                tools_provider=lambda c: dict(TOOLS), runtime_dir=Path("."))
        assert service.prepare() is None
        assert server.alive and server.starts == 1

        # A request is running (it holds the turn lock) when the background re-check is scheduled.
        service._turn_lock.acquire()
        recheck = threading.Thread(target=service.prepare, daemon=True)
        recheck.start()
        time.sleep(0.2)
        assert recheck.is_alive(), "the re-check should be waiting for the running request"

        # The user switches to local mode: the service is closed and its child stopped.
        service.close()
        assert not server.alive
        service._turn_lock.release()
        recheck.join(5.0)
        wait_for(lambda: service._spare_idle.is_set(), 2.0)

        assert not server.alive and server.starts == 1, (
            f"a closed Codex service started its child again (starts={server.starts}, alive={server.alive}); "
            "nothing will ever close it")
