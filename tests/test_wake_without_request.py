"""A wake (spoken or from the orb) that no request follows ends cleanly, and the next request is handled normally."""

import time
from unittest.mock import patch

import pytest

from jarvis import assistant_state
from jarvis.assistant_state import AssistantState
from tests.test_hot_window_input import _create_listener, _install_intent_judge, _make_judgment

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


# ---------------------------------------------------------------------------
# A wake with no request
# ---------------------------------------------------------------------------

def test_a_bare_wake_word_with_no_request_returns_to_idle(face_states):
    listener, _tts = _create_listener()
    listener.state_manager.wake_wait_seconds = 0.1
    _hear(listener, "Jarvis.")
    assert face_states[-1] is AssistantState.LISTENING

    time.sleep(0.2)
    listener._check_query_timeout()

    assert not listener.state_manager.is_collecting()
    assert face_states[-1] is AssistantState.IDLE
    listener.state_manager.stop()


def test_an_orb_wake_with_no_request_returns_to_idle(face_states):
    listener, _tts = _create_listener()
    listener.state_manager.wake_wait_seconds = 0.1
    listener.toggle_manual_wake()
    listener._check_query_timeout()
    assert listener.state_manager.is_collecting()

    time.sleep(0.2)
    listener._check_query_timeout()

    assert face_states[-1] is AssistantState.IDLE
    listener.state_manager.stop()


def test_the_request_after_an_unanswered_wake_is_judged_like_any_other(face_states):
    listener, _tts = _create_listener()
    listener.state_manager.wake_wait_seconds = 0.1
    _hear(listener, "Jarvis.")
    time.sleep(0.2)
    listener._check_query_timeout()
    judge = _install_intent_judge(listener, _make_judgment(query="what time is it"))

    with patch("jarvis.fastpath.dispatcher.match_command", return_value=None) as fast_path:
        _hear(listener, "um so jarvis what time is it")

    assert fast_path.called
    assert judge.judge.called
    assert listener.state_manager.get_pending_query() == "what time is it"
    listener.state_manager.stop()
