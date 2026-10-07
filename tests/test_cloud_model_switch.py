"""Changing the Claude or Codex model and effort while Jarvis runs (the web chat's model picker).

The daemon validates the choice against what the active bridge reports, saves it, and has the bridge
use it from the next request. Voice uses the same settings. See ``webchat/webchat.spec.md``, Models.
"""
import dataclasses
import threading
from types import SimpleNamespace

import pytest

from jarvis import daemon
from jarvis.bridge import modes, runtime
from jarvis.bridge.model_catalog import CloudModel, Effort


@dataclasses.dataclass(frozen=True)
class Cfg:
    llm_provider: str = "ollama"
    llm_chat_model: str = "gemma4:12b"
    claude_model: str = "sonnet"
    claude_effort: str = "low"
    codex_model: str = "gpt-6-luna"
    codex_reasoning_effort: str = "low"


def effort(*ids, default=None):
    return tuple(Effort(i, is_default=i == default) for i in ids)


SONNET = CloudModel("sonnet", "Sonnet", effort("low", "medium", "high", default="medium"))
HAIKU = CloudModel("haiku", "Haiku", ())
LUNA = CloudModel("gpt-6-luna", "GPT-6 Luna", effort("low", "medium", "high"))
SOL = CloudModel("gpt-6-sol", "GPT-6 Sol", effort("medium", "high", "xhigh", default="high"))


class FakeService:
    def __init__(self, models, accept=True):
        self.models, self.accept = models, accept
        self.configs = []

    def available_models(self):
        return list(self.models)

    def reconfigure(self, cfg):
        self.configs.append(cfg)
        return self.accept


@pytest.fixture
def world(monkeypatch):
    listener = SimpleNamespace(cfg=Cfg())
    saved = []
    rechecked = []
    state = SimpleNamespace(mode="claude", service=FakeService([SONNET, HAIKU]), save_ok=True)
    monkeypatch.setattr(daemon, "_global_cfg", Cfg())
    monkeypatch.setattr(daemon, "_global_voice_listener", listener)
    monkeypatch.setattr(daemon, "_chat_query_lock", threading.Lock())
    monkeypatch.setattr(modes, "active_mode", lambda: state.mode)
    monkeypatch.setattr(modes, "_cfg", Cfg())
    monkeypatch.setattr(modes, "recheck_active", lambda: rechecked.append(1))
    monkeypatch.setattr(runtime, "get_service", lambda: state.service)
    monkeypatch.setattr("jarvis.config.update_config_values",
                        lambda values: saved.append(values) or state.save_ok)
    return SimpleNamespace(state=state, listener=listener, saved=saved, rechecked=rechecked)


class TestClaude:
    def test_a_model_and_effort_reach_the_daemon_the_voice_listener_and_the_bridge(self, world):
        result = daemon.set_cloud_model("sonnet", "high")
        assert result.ok and result.mode == "claude" and result.model == "sonnet" and result.effort == "high"
        assert daemon._global_cfg.claude_effort == "high"
        assert world.listener.cfg.claude_effort == "high"
        assert modes._cfg.claude_effort == "high"
        assert world.state.service.configs[-1].claude_effort == "high"

    def test_the_choice_is_saved_for_the_next_start(self, world):
        daemon.set_cloud_model("sonnet", "high")
        assert world.saved == [{"claude_model": "sonnet", "claude_effort": "high"}]

    def test_the_bridge_checks_the_new_choice_in_the_background(self, world):
        daemon.set_cloud_model("sonnet", "high")
        assert world.rechecked == [1]

    def test_a_model_without_effort_levels_clears_the_effort(self, world):
        result = daemon.set_cloud_model("haiku", None)
        assert result.ok and result.effort == ""
        assert world.saved == [{"claude_model": "haiku", "claude_effort": ""}]

    def test_an_omitted_effort_keeps_the_current_one_when_the_model_offers_it(self, world):
        assert daemon.set_cloud_model("sonnet", None).effort == "low"

    def test_an_omitted_effort_falls_back_to_the_models_default(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", Cfg(claude_effort="max"))
        assert daemon.set_cloud_model("sonnet", None).effort == "medium"

    def test_an_effort_the_model_does_not_offer_is_refused(self, world):
        result = daemon.set_cloud_model("sonnet", "max")
        assert not result.ok and result.reason == "effort_unsupported" and world.saved == []

    def test_an_effort_for_a_model_with_none_is_refused(self, world):
        assert daemon.set_cloud_model("haiku", "low").reason == "effort_unsupported"

    def test_choosing_what_is_already_in_use_changes_nothing(self, world):
        result = daemon.set_cloud_model("sonnet", "low")
        assert result.ok and result.reason == "already" and world.saved == [] and world.rechecked == []


class TestCodex:
    @pytest.fixture(autouse=True)
    def _codex(self, world):
        world.state.mode = "codex"
        world.state.service = FakeService([LUNA, SOL])

    def test_codex_settings_use_the_codex_keys(self, world):
        result = daemon.set_cloud_model("gpt-6-sol", "xhigh")
        assert result.ok and result.mode == "codex"
        assert world.saved == [{"codex_model": "gpt-6-sol", "codex_reasoning_effort": "xhigh"}]
        assert daemon._global_cfg.codex_model == "gpt-6-sol"
        assert daemon._global_cfg.claude_model == "sonnet"  # the other mode's choice is untouched

    def test_codex_uses_the_models_default_effort_when_the_current_one_is_not_offered(self, world):
        assert daemon.set_cloud_model("gpt-6-sol", None).effort == "high"


class TestRefusals:
    def test_local_mode_has_no_cloud_model(self, world):
        world.state.mode = "local"
        result = daemon.set_cloud_model("sonnet", "low")
        assert not result.ok and result.reason == "not_cloud"

    def test_before_the_bridge_has_reported_its_models(self, world):
        world.state.service = FakeService([])
        assert daemon.set_cloud_model("sonnet", "low").reason == "not_ready"

    def test_without_a_bridge(self, world):
        world.state.service = None
        assert daemon.set_cloud_model("sonnet", "low").reason == "not_ready"

    def test_a_model_the_bridge_does_not_offer(self, world):
        result = daemon.set_cloud_model("nope", "low")
        assert not result.ok and result.reason == "not_offered" and world.saved == []

    def test_while_a_query_is_running(self, world):
        daemon._chat_query_lock.acquire()
        try:
            result = daemon.set_cloud_model("sonnet", "high")
        finally:
            daemon._chat_query_lock.release()
        assert not result.ok and result.reason == "busy" and world.saved == []

    def test_while_the_bridge_is_busy(self, world):
        world.state.service = FakeService([SONNET], accept=False)
        result = daemon.set_cloud_model("sonnet", "high")
        assert not result.ok and result.reason == "busy"
        assert world.saved == [] and daemon._global_cfg.claude_effort == "low"

    def test_a_failed_save_puts_the_old_settings_back(self, world):
        world.state.save_ok = False
        result = daemon.set_cloud_model("sonnet", "high")
        assert not result.ok and result.reason == "save_failed"
        assert daemon._global_cfg.claude_effort == "low"
        assert world.state.service.configs[-1].claude_effort == "low"

    def test_before_the_daemon_has_booted(self, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", None)
        assert daemon.set_cloud_model("sonnet", "low").reason == "not_ready"
