"""A fast command heard in the follow-up window closes that window instead of letting it expire mid-reply."""

import time
from unittest.mock import patch

import pytest

from jarvis import assistant_state
from jarvis.assistant_state import AssistantState
from jarvis.listening.state_manager import ListeningState
from tests.test_hot_window_input import _create_listener

pytestmark = pytest.mark.unit


@pytest.fixture
def face_states():
    assistant_state.reset()
    states = []
    assistant_state.subscribe(states.append)
    yield states
    assistant_state.reset()


def _hear(listener, text, *, started_ago=1.0, ended_ago=0.5):
    now = time.time()
    listener._process_transcript(text, 0.01, now - started_ago, now - ended_ago,
                                 captured_during_tts=False, captured_tts_start_time=0.0)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# A fast command answered in the follow-up window
# ---------------------------------------------------------------------------

def test_a_fast_command_in_the_follow_up_window_ends_the_window(face_states, capsys):
    listener, _tts = _create_listener(hot_window_seconds=0.4, echo_tolerance=0.05)
    listener.activate_hot_window()
    assert _wait_until(listener.state_manager.is_hot_window_active)
    dispatched = []
    listener._dispatch_query = lambda query: (
        dispatched.append(query), assistant_state.set_state(AssistantState.THINKING))

    match = type("Match", (), {"family": "media", "tool_name": "mediaControl"})()
    with patch("jarvis.fastpath.dispatcher.match_command", return_value=match):
        _hear(listener, "pause the music", started_ago=0.1, ended_ago=0.0)

    assert dispatched == ["pause the music"]
    assert listener.state_manager.get_state() == ListeningState.WAKE_WORD
    capsys.readouterr()
    time.sleep(0.6)  # past the follow-up window's original expiry, reply still being generated
    assert face_states[-1] is AssistantState.THINKING
    assert "Returning to wake word mode" not in capsys.readouterr().out
    listener.state_manager.stop()
