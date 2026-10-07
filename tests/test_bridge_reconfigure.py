"""Changing the Codex or Claude model and effort while Jarvis runs (the web chat's model picker).

A bridge keeps what its runtime reports it can do (``available_models``), and ``reconfigure`` makes the
next request use a new model and effort: the readiness check runs again, a spare session or thread started
with the old model is dropped, and a request in flight is never touched. See ``bridge/bridge.spec.md``.
"""
import threading
from pathlib import Path

import pytest

from claude_bridge_fakes import FakeClaude, SIGNED_IN
from claude_bridge_fakes import make_cfg as claude_cfg
from claude_bridge_fakes import scripted as claude_scripted
from codex_bridge_fakes import FakeAppServer, FakeExecutor, FakeStore, LUNA
from codex_bridge_fakes import make_cfg as codex_cfg
from codex_bridge_fakes import scripted as codex_scripted
from jarvis.claude_bridge.service import ClaudeBridgeService
from jarvis.codex_bridge.service import BridgeService

TOOLS = {"getTime": {"description": "time", "inputSchema": {"type": "object", "properties": {}, "required": []}}}
SOL = {"id": "gpt-6-sol", "model": "gpt-6-sol", "displayName": "GPT-6 Sol",
       "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("medium", "high", "xhigh")]}


def codex(script=None, cfg=None, models=None):
    server = FakeAppServer(script, models=models if models is not None else [LUNA, SOL])
    service = BridgeService(cfg or codex_cfg(), server, executor=FakeExecutor(), confirmation_store=FakeStore(),
                            tools_provider=lambda c: dict(TOOLS), runtime_dir=Path("."))
    return service, server


def claude(script=None, cfg=None):
    factory = FakeClaude(script)
    service = ClaudeBridgeService(cfg or claude_cfg(), factory, executor=FakeExecutor(), confirmation_store=FakeStore(),
                                  tools_provider=lambda c: dict(TOOLS), auth_reader=lambda: dict(SIGNED_IN),
                                  instructions_provider=lambda: "CONTRACT")
    return service, factory


def ask(service):
    return service.run_request("open word", context=[], origin="voice", language="en", db=None, quiet=False)


def wait_for(predicate, timeout=3.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.unit
class TestCodex:
    def ok(self):
        return codex_scripted(("answer", "completed", "done"))

    def test_the_models_are_known_once_the_runtime_has_been_checked(self):
        service, _ = codex(self.ok())
        assert service.available_models() == []
        assert service.prepare() is None
        assert [m.id for m in service.available_models()] == ["gpt-6-luna", "gpt-6-sol"]
        assert [e.id for e in service.available_models()[1].efforts] == ["medium", "high", "xhigh"]
        service.close()

    def test_the_next_request_uses_the_new_model_and_effort(self):
        service, server = codex(self.ok())
        service.prepare()
        assert service.reconfigure(codex_cfg(codex_model="gpt-6-sol", codex_reasoning_effort="high")) is True
        assert ask(service).kind == "reply"
        assert server.params_of("thread/start")[-1]["model"] == "gpt-6-sol"
        assert server.params_of("turn/start")[-1]["effort"] == "high"
        service.close()

    def test_a_spare_thread_started_with_the_old_model_is_not_used(self):
        service, server = codex(self.ok())
        service.prepare()
        assert wait_for(lambda: len(server.params_of("thread/start")) == 1)
        spare = next(iter(server.threads))
        service.reconfigure(codex_cfg(codex_model="gpt-6-sol", codex_reasoning_effort="high"))
        ask(service)
        used = server.params_of("turn/start")[-1]["threadId"]
        assert used != spare
        assert server.threads[used]["model"] == "gpt-6-sol"
        assert spare in server.unsubscribed
        service.close()

    def test_a_model_the_account_does_not_have_fails_clearly_and_is_never_substituted(self):
        service, server = codex(self.ok())
        service.prepare()
        service.reconfigure(codex_cfg(codex_model="gpt-9-none", codex_reasoning_effort="low"))
        outcome = ask(service)
        assert (outcome.kind, outcome.reason) == ("error", "model_unavailable")
        assert server.params_of("turn/start") == []
        service.close()

    def test_an_effort_the_model_does_not_offer_fails_clearly(self):
        service, _ = codex(self.ok())
        service.prepare()
        service.reconfigure(codex_cfg(codex_model="gpt-6-luna", codex_reasoning_effort="xhigh"))
        assert ask(service).reason == "effort_unsupported"
        service.close()

    def test_nothing_changes_while_a_request_is_running(self):
        started, release = threading.Event(), threading.Event()

        def slow(model):
            started.set()
            release.wait(5)
            model.answer("completed", "done")

        service, server = codex(slow)
        worker = threading.Thread(target=lambda: ask(service), daemon=True)
        worker.start()
        assert started.wait(3)
        assert service.reconfigure(codex_cfg(codex_model="gpt-6-sol", codex_reasoning_effort="high")) is False
        release.set()
        worker.join(5)
        assert server.params_of("thread/start")[0]["model"] == "gpt-6-luna"
        service.close()


@pytest.mark.unit
class TestClaude:
    def ok(self):
        return claude_scripted(("answer", "completed", "done"))

    def test_the_models_are_known_once_the_runtime_has_been_checked(self):
        service, _ = claude(self.ok())
        assert service.available_models() == []
        assert service.prepare() is None
        models = {m.id: m for m in service.available_models()}
        assert set(models) == {"default", "sonnet", "haiku"}
        assert [e.id for e in models["sonnet"].efforts] == ["low", "medium", "high", "xhigh", "max"]
        assert models["haiku"].efforts == ()
        service.close()

    def test_the_next_request_uses_the_new_model_and_effort(self):
        service, factory = claude(self.ok())
        service.prepare()
        assert service.reconfigure(claude_cfg(claude_model="default", claude_effort="high")) is True
        assert ask(service).kind == "reply"
        used = factory.used()[0]
        assert used.arg("--model") == "default" and used.arg("--effort") == "high"
        service.close()

    def test_a_model_without_effort_levels_gets_no_effort_flag(self):
        service, factory = claude(self.ok())
        service.prepare()
        service.reconfigure(claude_cfg(claude_model="haiku", claude_effort=""))
        ask(service)
        assert factory.used()[0].arg("--model") == "haiku" and factory.used()[0].arg("--effort") is None
        service.close()

    def test_a_spare_session_started_with_the_old_model_is_stopped_and_not_used(self):
        service, factory = claude(self.ok())
        service.prepare()
        assert wait_for(lambda: len([s for s in factory.started if not s.user_texts]) >= 2)
        service.reconfigure(claude_cfg(claude_model="default", claude_effort="medium"))
        ask(service)
        used = factory.used()[0]
        assert used.arg("--model") == "default"
        old_spares = [s for s in factory.started if s.arg("--model") == "sonnet" and not s.user_texts]
        assert old_spares and all(s.closes >= 1 for s in old_spares)
        service.close()

    def test_a_model_the_cli_does_not_offer_fails_clearly_and_is_never_substituted(self):
        service, factory = claude(self.ok())
        service.prepare()
        service.reconfigure(claude_cfg(claude_model="nope", claude_effort="low"))
        outcome = ask(service)
        assert (outcome.kind, outcome.reason) == ("error", "model_unavailable")
        assert factory.used() == []
        service.close()

    def test_an_effort_the_model_does_not_offer_fails_clearly(self):
        service, _ = claude(self.ok())
        service.prepare()
        service.reconfigure(claude_cfg(claude_model="sonnet", claude_effort="ultra"))
        assert ask(service).reason == "effort_unsupported"
        service.close()

    def test_nothing_changes_while_a_request_is_running(self):
        started, release = threading.Event(), threading.Event()

        def slow(model):
            started.set()
            release.wait(5)
            model.answer("completed", "done")

        service, factory = claude(slow)
        worker = threading.Thread(target=lambda: ask(service), daemon=True)
        worker.start()
        assert started.wait(3)
        assert service.reconfigure(claude_cfg(claude_model="default", claude_effort="high")) is False
        release.set()
        worker.join(5)
        assert factory.used()[0].arg("--model") == "sonnet"
        service.close()
