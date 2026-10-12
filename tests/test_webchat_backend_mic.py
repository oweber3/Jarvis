"""The web chat's microphone door into the daemon: the dictation engine, and when Jarvis is too busy.

The daemon's engine, settings and state are replaced; no microphone is opened.
"""
from types import SimpleNamespace

import pytest

from jarvis import assistant_state, daemon
from jarvis.assistant_state import AssistantState
from jarvis.dictation.dictation_engine import CaptureRefused
from jarvis.webchat.backend import DaemonBackend


class FakeEngine:
    def __init__(self):
        self.blocker = None
        self.refuse = None
        self.open = None
        self.calls = []

    def capture_blocker(self):
        return self.blocker

    def start_capture(self, max_seconds):
        self.calls.append(("start", max_seconds))
        if self.refuse:
            raise CaptureRefused(self.refuse)
        self.open = 7
        return 7

    def capture_state(self, token):
        return "recording" if token == self.open else None

    def stop_capture(self, token):
        self.calls.append(("stop", token))
        if token != self.open:
            return None
        self.open = None
        return "spoken words"

    def cancel_capture(self, token):
        self.calls.append(("cancel", token))
        was, self.open = self.open == token, None
        return was


@pytest.fixture
def world(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(daemon, "_global_dictation_engine", engine)
    monkeypatch.setattr(daemon, "_global_cfg", SimpleNamespace(dictation_enabled=True))
    monkeypatch.setattr(assistant_state, "_state", AssistantState.IDLE)
    monkeypatch.setattr(daemon, "is_query_running", lambda: False)
    return SimpleNamespace(engine=engine, backend=DaemonBackend())


def test_an_idle_jarvis_with_dictation_can_record(world):
    assert world.backend.mic_reason() is None


def test_dictation_turned_off_in_settings_is_its_own_reason(world, monkeypatch):
    monkeypatch.setattr(daemon, "_global_cfg", SimpleNamespace(dictation_enabled=False))
    assert world.backend.mic_reason() == "disabled"


def test_no_engine_means_unavailable(world, monkeypatch):
    monkeypatch.setattr(daemon, "_global_dictation_engine", None)
    assert world.backend.mic_reason() == "unavailable"
    assert world.backend.mic_start(30) == (None, "unavailable")


def test_a_running_request_makes_jarvis_busy(world, monkeypatch):
    monkeypatch.setattr(daemon, "is_query_running", lambda: True)
    assert world.backend.mic_reason() == "busy"
    assert world.backend.mic_start(30) == (None, "busy")
    assert world.engine.calls == []


@pytest.mark.parametrize("state", [s for s in AssistantState if s is not AssistantState.IDLE])
def test_anything_but_idle_is_busy(world, monkeypatch, state):
    monkeypatch.setattr(assistant_state, "_state", state)
    assert world.backend.mic_reason() == "busy"


def test_the_engines_own_reason_is_passed_on(world):
    world.engine.blocker = "not_ready"
    assert world.backend.mic_reason() == "not_ready"


def test_a_refusal_at_the_microphone_is_a_reason_not_an_error(world):
    world.engine.refuse = "microphone"
    assert world.backend.mic_start(30) == (None, "microphone")


def test_start_stop_and_cancel_go_to_the_engine(world):
    handle, reason = world.backend.mic_start(45)
    assert (handle, reason) == (7, None) and world.engine.calls == [("start", 45)]
    assert world.backend.mic_state(handle) == "recording"
    assert world.backend.mic_stop(handle) == "spoken words"
    assert world.backend.mic_stop(handle) is None
    handle, _ = world.backend.mic_start(45)
    assert world.backend.mic_cancel(handle) is True


def test_a_stopped_daemon_has_no_recording(world, monkeypatch):
    handle, _ = world.backend.mic_start(30)
    monkeypatch.setattr(daemon, "_global_dictation_engine", None)
    assert world.backend.mic_state(handle) is None
    assert world.backend.mic_stop(handle) is None
    assert world.backend.mic_cancel(handle) is False
