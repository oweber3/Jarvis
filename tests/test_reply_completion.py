"""When Jarvis finishes speaking: the orb, echo timing and the follow-up window, for every spoken reply."""

import threading
import time

import pytest

from jarvis import assistant_state
from jarvis.assistant_state import AssistantState
from jarvis.listening.state_manager import ListeningState
from tests.audio_harness import ListenerHarness, fixture, pad, silence
from tests.test_hot_window_input import _create_listener

pytestmark = pytest.mark.unit


@pytest.fixture
def face_states():
    assistant_state.reset()
    states = []
    assistant_state.subscribe(states.append)
    yield states
    assistant_state.reset()


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Replies with the follow-up window switched off, and the error apology
# ---------------------------------------------------------------------------

def _speak_and_capture_completion(listener, tts, speak):
    callbacks = {}
    tts.speak.side_effect = lambda text, completion_callback=None, **_: callbacks.setdefault(
        "complete", completion_callback)
    speak()
    assistant_state.set_state(AssistantState.SPEAKING)  # what the TTS engine reports when it starts
    return callbacks.get("complete")


def test_the_orb_returns_to_idle_after_a_reply_when_the_follow_up_window_is_off(face_states):
    listener, tts = _create_listener()
    listener.cfg.hot_window_enabled = False

    complete = _speak_and_capture_completion(listener, tts, lambda: listener._speak_reply("It is sunny."))
    complete()

    assert face_states[-1] is AssistantState.IDLE
    assert listener.state_manager.get_state() == ListeningState.WAKE_WORD
    listener.state_manager.stop()


def test_echo_just_after_a_reply_is_not_a_wake_when_the_follow_up_window_is_off():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    reply = f"You asked {clip.text}, and it is ten past four."
    with ListenerHarness({"hot_window_enabled": False}, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start(reply)
        harness.play(silence(0.5))
        harness.tts.speaking = False
        harness.listener.activate_hot_window()  # the TTS completion callback
        harness.play(pad(clip.audio, after=0.2), text=clip.text)  # the room's echo of the reply's end
        harness.play(silence(2.0))

    assert harness.collections == []
    assert harness.dispatched == []


def test_the_error_apology_opens_the_follow_up_window_like_any_reply(monkeypatch, face_states):
    from jarvis.reply import engine as engine_module

    def failing_engine(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(engine_module, "run_reply_engine", failing_engine)
    listener, tts = _create_listener(echo_tolerance=0.05)

    complete = _speak_and_capture_completion(
        listener, tts, lambda: listener._generate_reply("what time is it", "en", None, threading.Event()))
    assert complete is not None, "the apology was spoken without a completion callback"
    complete()

    assert _wait_until(listener.state_manager.is_hot_window_active)
    listener.state_manager.stop()
