"""The web chat's only door into Jarvis: what it asks of the daemon, the reply modes and the models.

The daemon module's functions are replaced; nothing here starts a model or a bridge.
"""
import dataclasses
from types import SimpleNamespace

import pytest

from jarvis import daemon
from jarvis.bridge import modes
from jarvis.config import OFFERED_CHAT_MODELS
from jarvis.memory.conversation import DialogueMemory
from jarvis.webchat.backend import DaemonBackend


@dataclasses.dataclass(frozen=True)
class Cfg:
    llm_provider: str = "ollama"
    llm_chat_model: str = "gemma4:12b"


class FakeLLM:
    def __init__(self, installed):
        self.installed = installed
        self.calls = 0

    def list_models(self, **_kw):
        self.calls += 1
        return list(self.installed)


@pytest.fixture
def world(monkeypatch):
    llm = FakeLLM(["gemma4:12b"])
    monkeypatch.setattr(daemon, "_global_cfg", Cfg())
    monkeypatch.setattr(daemon, "_global_dialogue_memory", DialogueMemory())
    monkeypatch.setattr("jarvis.llm.get_llm_backend", lambda cfg: llm)
    return SimpleNamespace(llm=llm)


def test_ready_only_once_the_daemon_has_booted(world, monkeypatch):
    assert DaemonBackend().is_ready() is True
    monkeypatch.setattr(daemon, "_global_dialogue_memory", None)
    assert DaemonBackend().is_ready() is False


def test_typed_requests_go_through_the_text_path_as_chat(world, monkeypatch):
    seen = {}
    monkeypatch.setattr(daemon, "submit_text_query", lambda text, **kw: seen.update(text=text, **kw))
    DaemonBackend().submit("hello", on_start=print, on_complete=print, on_busy=print)
    assert seen["text"] == "hello" and seen["origin"] == "chat"
    assert seen["on_start"] is print and seen["on_busy"] is print


def test_stop_cancels_the_chat_query(world, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "cancel_active_chat_query", lambda: calls.append(1))
    DaemonBackend().cancel()
    assert calls == [1]


def test_the_conversation_swap_is_the_daemons(world, monkeypatch):
    monkeypatch.setattr(daemon, "switch_chat_conversation", lambda messages: messages == [{"role": "user", "content": "x"}])
    assert DaemonBackend().switch_conversation([{"role": "user", "content": "x"}]) is True
    assert DaemonBackend().switch_conversation([]) is False


class TestReplyModes:
    def test_the_state_comes_from_the_mode_registry(self, world, monkeypatch):
        monkeypatch.setattr(modes, "state", lambda: {"mode": "local", "enabled": ["local", "claude"]})
        assert DaemonBackend().reply_mode_state() == {"mode": "local", "enabled": ["local", "claude"]}

    def test_a_missing_registry_reads_as_no_state(self, world, monkeypatch):
        def boom():
            raise RuntimeError

        monkeypatch.setattr(modes, "state", boom)
        assert DaemonBackend().reply_mode_state() is None

    def test_a_switch_reports_success(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "set_reply_mode", lambda mode: modes.SwitchResult(True, mode, "local"))
        assert DaemonBackend().switch_reply_mode("claude") == (True, None)

    def test_choosing_the_current_mode_is_a_success(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "set_reply_mode", lambda mode: modes.SwitchResult(True, mode, mode, "already"))
        assert DaemonBackend().switch_reply_mode("local") == (True, None)

    def test_a_refusal_carries_its_reason(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "set_reply_mode", lambda mode: modes.SwitchResult(False, "local", "local", "not_enabled"))
        assert DaemonBackend().switch_reply_mode("claude") == (False, "not_enabled")


class TestLocalModels:
    def test_the_current_model_is_reported(self, world):
        assert DaemonBackend().local_model_state() == {"current": "gemma4:12b", "switchable": True}

    def test_only_the_ollama_provider_can_switch(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", Cfg(llm_provider="openai_compatible", llm_chat_model="my-model"))
        assert DaemonBackend().local_model_state() == {"current": "my-model", "switchable": False}
        assert [m["id"] for m in DaemonBackend().local_models()] == ["my-model"]

    def test_no_settings_means_no_model(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", None)
        assert DaemonBackend().local_model_state() == {"current": None, "switchable": False}
        assert DaemonBackend().local_models() == []

    def test_every_offered_model_is_listed_with_whether_it_is_installed(self, world):
        listed = {m["id"]: m for m in DaemonBackend().local_models()}
        assert set(listed) == set(OFFERED_CHAT_MODELS)
        assert listed["gemma4:12b"]["installed"] is True
        other = next(m for m in OFFERED_CHAT_MODELS if m != "gemma4:12b")
        assert listed[other]["installed"] is False
        assert listed["gemma4:12b"]["name"] == OFFERED_CHAT_MODELS["gemma4:12b"]["name"]

    def test_a_model_set_by_hand_is_listed_as_the_current_one(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", Cfg(llm_chat_model="my-custom:7b"))
        listed = {m["id"]: m for m in DaemonBackend().local_models()}
        assert listed["my-custom:7b"] == {"id": "my-custom:7b", "name": "my-custom:7b", "installed": True}

    def test_the_runtime_is_not_asked_again_within_the_cache_time(self, world):
        now = [100.0]
        backend = DaemonBackend(clock=lambda: now[0])
        backend.local_models()
        backend.local_models()
        assert world.llm.calls == 1
        now[0] += 60
        backend.local_models()
        assert world.llm.calls == 2

    def test_an_unreachable_runtime_lists_nothing_as_installed(self, world, monkeypatch):
        class Down:
            def list_models(self, **_kw):
                raise ConnectionError

        monkeypatch.setattr("jarvis.llm.get_llm_backend", lambda cfg: Down())
        assert all(not m["installed"] for m in DaemonBackend().local_models() if m["id"] != "gemma4:12b")

    def test_a_switch_is_the_daemons(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "set_local_chat_model",
                            lambda model: daemon.LocalModelResult(True, model, None))
        assert DaemonBackend().set_local_model("qwen3.5:9b") == (True, None)

    def test_choosing_the_current_model_is_a_success(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "set_local_chat_model",
                            lambda model: daemon.LocalModelResult(True, model, "already"))
        assert DaemonBackend().set_local_model("gemma4:12b") == (True, None)

    def test_a_model_refusal_carries_its_reason(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "set_local_chat_model",
                            lambda model: daemon.LocalModelResult(False, "gemma4:12b", "not_installed"))
        assert DaemonBackend().set_local_model("x") == (False, "not_installed")
