"""Changing the local chat model while Jarvis runs (the web chat's model selector).

The model changes for the whole assistant, voice included: the daemon's settings and the voice
listener's are replaced, the choice is saved, and the old model is released. See
``webchat/webchat.spec.md``, Models.
"""
import dataclasses
import threading
from types import SimpleNamespace

import pytest

from jarvis import daemon
from jarvis.bridge import modes
from jarvis.config import OFFERED_CHAT_MODELS


@dataclasses.dataclass(frozen=True)
class Cfg:
    llm_provider: str = "ollama"
    llm_chat_model: str = "gemma4:12b"
    ollama_chat_model: str = "gemma4:12b"
    fast_model: str = "qwen3.5:0.8b"
    embedding_model: str = "nomic-embed-text"
    tool_model: str = ""


class FakeBackend:
    def __init__(self, installed):
        self.installed = installed
        self.released, self.warmed = [], []
        self.done = threading.Event()

    def list_models(self, **_kw):
        return list(self.installed)

    def warm_up(self, model, **_kw):
        self.warmed.append(model)
        return True

    def release(self, model, **_kw):
        self.released.append(model)
        self.done.set()
        return True


OTHER = next(m for m in OFFERED_CHAT_MODELS if m != "gemma4:12b" and m != "qwen3.5:0.8b")


@pytest.fixture
def world(monkeypatch):
    listener = SimpleNamespace(cfg=Cfg())
    backend = FakeBackend(installed=["gemma4:12b", OTHER, "qwen3.5:0.8b", "nomic-embed-text"])
    saved = []
    monkeypatch.setattr(daemon, "_global_cfg", Cfg())
    monkeypatch.setattr(daemon, "_global_voice_listener", listener)
    monkeypatch.setattr(daemon, "_chat_query_lock", threading.Lock())
    monkeypatch.setattr("jarvis.llm.get_llm_backend", lambda cfg: backend)
    monkeypatch.setattr("jarvis.config.update_config_values", lambda values: saved.append(values) or True)
    monkeypatch.setattr(modes, "_cfg", Cfg())
    return SimpleNamespace(listener=listener, backend=backend, saved=saved)


def test_the_new_model_reaches_the_daemon_and_the_voice_listener(world):
    result = daemon.set_local_chat_model(OTHER)
    assert result.ok and result.model == OTHER
    assert daemon._global_cfg.llm_chat_model == OTHER and daemon._global_cfg.ollama_chat_model == OTHER
    assert world.listener.cfg.llm_chat_model == OTHER


def test_the_choice_is_saved_for_the_next_start(world):
    daemon.set_local_chat_model(OTHER)
    assert world.saved == [{"ollama_chat_model": OTHER}]


def test_the_old_model_is_released_and_the_new_one_warmed(world):
    daemon.set_local_chat_model(OTHER)
    assert world.backend.done.wait(5)
    assert world.backend.released == ["gemma4:12b"] and world.backend.warmed == [OTHER]


def test_a_model_the_fast_tier_shares_is_not_released(world, monkeypatch):
    shared = Cfg(fast_model="gemma4:12b")
    monkeypatch.setattr(daemon, "_global_cfg", shared)
    daemon.set_local_chat_model(OTHER)
    assert world.backend.released == []


def test_choosing_the_current_model_changes_nothing(world):
    result = daemon.set_local_chat_model("gemma4:12b")
    assert result.ok and result.reason == "already"
    assert world.saved == []


@pytest.mark.parametrize("name", ["", "   ", "not-a-real-model"])
def test_a_model_jarvis_does_not_offer_is_refused(world, name):
    result = daemon.set_local_chat_model(name)
    assert not result.ok and result.reason == "not_offered"
    assert daemon._global_cfg.llm_chat_model == "gemma4:12b" and world.saved == []


def test_a_model_that_is_not_installed_is_refused(world):
    world.backend.installed = ["gemma4:12b"]
    result = daemon.set_local_chat_model(OTHER)
    assert not result.ok and result.reason == "not_installed"
    assert world.saved == []


def test_refused_while_a_query_is_running(world):
    daemon._chat_query_lock.acquire()
    try:
        result = daemon.set_local_chat_model(OTHER)
    finally:
        daemon._chat_query_lock.release()
    assert not result.ok and result.reason == "busy"
    assert daemon._global_cfg.llm_chat_model == "gemma4:12b"


def test_only_the_ollama_provider_can_be_switched(world, monkeypatch):
    monkeypatch.setattr(daemon, "_global_cfg", Cfg(llm_provider="openai_compatible"))
    result = daemon.set_local_chat_model(OTHER)
    assert not result.ok and result.reason == "unsupported_provider"


def test_a_failed_save_leaves_the_running_model_alone(world, monkeypatch):
    monkeypatch.setattr("jarvis.config.update_config_values", lambda values: False)
    result = daemon.set_local_chat_model(OTHER)
    assert not result.ok and result.reason == "save_failed"
    assert daemon._global_cfg.llm_chat_model == "gemma4:12b"


def test_the_reply_mode_registry_sees_the_new_settings(world):
    daemon.set_local_chat_model(OTHER)
    assert modes._cfg.llm_chat_model == OTHER


def test_refused_before_the_daemon_has_booted(monkeypatch):
    monkeypatch.setattr(daemon, "_global_cfg", None)
    result = daemon.set_local_chat_model(OTHER)
    assert not result.ok and result.reason == "not_ready"
