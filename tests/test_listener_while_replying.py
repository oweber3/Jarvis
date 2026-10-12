"""What the listener does while a reply is still being generated or another reply follows.

Wake-word speech heard during a reply is detected, judged and passed on as addressed like any
other; background speech does not touch the engagement or the face of the reply in progress;
and only the last of several replies arms the follow-up window or returns the face to idle.
"""

import threading
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


@pytest.fixture
def no_fast_path():
    with patch("jarvis.fastpath.dispatcher.match_command", return_value=None) as fast_path:
        yield fast_path


class StagedEngine:
    """Stands in for run_reply_engine: answers at once unless the query is held."""

    def __init__(self):
        self.calls = []
        self.held = {}  # query -> Event released by the test
        self.replies = {}  # query -> reply text (None for no reply)

    def hold(self, query):
        self.held[query] = threading.Event()

    def release(self, query):
        self.held[query].set()

    def __call__(self, db, cfg, tts, query, dialogue_memory, language=None, addressed=True, **_):
        self.calls.append((query, addressed))
        gate = self.held.get(query)
        if gate is not None:
            assert gate.wait(5), "engine never released"
        return self.replies.get(query, f"reply to {query}")


@pytest.fixture
def engine(monkeypatch):
    from jarvis.reply import engine as engine_module
    fake = StagedEngine()
    monkeypatch.setattr(engine_module, "run_reply_engine", fake)
    yield fake
    for gate in fake.held.values():
        gate.set()


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _hear(listener, text, *, started_ago=0.6, ended_ago=0.1):
    now = time.time()
    listener._transcript_buffer.add(text, now - started_ago, now - ended_ago, 0.01, False)
    listener._process_transcript(
        text, 0.01, now - started_ago, now - ended_ago,
        captured_during_tts=False,
        captured_tts_start_time=listener.echo_detector._tts_start_time,
    )


def _spoken(tts):
    """(text, completion callback) of every reply handed to TTS, in order."""
    return [(c.args[0], c.kwargs.get("completion_callback")) for c in tts.speak.call_args_list]


def _ask(listener, engine, query):
    """Ask Jarvis something with the wake word and let the collection pause pass."""
    _install_intent_judge(listener, _make_judgment(query=query))
    listener.state_manager.voice_collect_seconds = 0.05
    _hear(listener, f"jarvis {query}")
    assert listener.state_manager.is_collecting()
    time.sleep(0.1)
    listener._check_query_timeout()
    assert _wait_until(lambda: any(call[0] == query for call in engine.calls)), engine.calls


# ---------------------------------------------------------------------------
# Wake-word speech during a reply
# ---------------------------------------------------------------------------

def test_a_wake_word_request_during_a_reply_is_passed_on_as_addressed(face_states, engine, no_fast_path):
    listener, _tts = _create_listener()
    engine.hold("what's the weather")
    _ask(listener, engine, "what's the weather")

    _install_intent_judge(listener, _make_judgment(query="paste"))
    _hear(listener, "jarvis paste")
    time.sleep(0.1)
    listener._check_query_timeout()
    engine.release("what's the weather")

    assert _wait_until(lambda: len(engine.calls) == 2), engine.calls
    assert engine.calls[1] == ("paste", True)
    listener.stop()


def test_a_wake_word_request_during_a_reply_is_judged(face_states, engine, no_fast_path):
    listener, _tts = _create_listener()
    engine.hold("what's the weather")
    _ask(listener, engine, "what's the weather")

    judge = _install_intent_judge(listener, _make_judgment(query="what is the capital of france"))
    _hear(listener, "um jarvis what's the capital of france")

    assert judge.judge.called
    assert listener.state_manager.get_pending_query() == "what is the capital of france"
    listener.stop()


def test_a_wake_word_command_during_a_reply_takes_the_fast_path(face_states, engine):
    listener, _tts = _create_listener()
    engine.hold("what's the weather")
    with patch("jarvis.fastpath.dispatcher.match_command", return_value=None):
        _ask(listener, engine, "what's the weather")

    fast_match = type("Match", (), {"family": "edit", "tool_name": "inputControl"})()
    with patch("jarvis.fastpath.dispatcher.match_command", return_value=fast_match) as fast_path:
        _hear(listener, "jarvis paste")
    engine.release("what's the weather")

    assert fast_path.call_args.kwargs["addressed"] is True
    assert _wait_until(lambda: len(engine.calls) == 2), engine.calls
    assert engine.calls[1] == ("paste", True)
    listener.stop()


# ---------------------------------------------------------------------------
# Background speech during a reply
# ---------------------------------------------------------------------------

def test_background_speech_during_a_reply_keeps_the_orb_thinking(face_states, engine, no_fast_path):
    listener, _tts = _create_listener()
    engine.hold("what's the weather")
    _ask(listener, engine, "what's the weather")
    assert face_states[-1] is AssistantState.THINKING

    _hear(listener, "the kettle has boiled")

    assert face_states[-1] is AssistantState.THINKING
    listener.stop()


# ---------------------------------------------------------------------------
# Several replies in a row
# ---------------------------------------------------------------------------

def _first_reply_spoken_while_second_is_generated(listener, tts, engine):
    """The user asks a second question while Jarvis is still answering the first."""
    _ask(listener, engine, "tell me the news")
    assert _wait_until(lambda: len(_spoken(tts)) == 1)
    engine.hold("what's the weather")
    _ask(listener, engine, "what's the weather")
    return _spoken(tts)[0][1]


def test_the_end_of_an_earlier_reply_does_not_open_the_hot_window_while_the_next_is_generated(
        face_states, engine, no_fast_path):
    listener, tts = _create_listener(hot_window_seconds=0.3, echo_tolerance=0.05)
    first_finished = _first_reply_spoken_while_second_is_generated(listener, tts, engine)

    first_finished()
    time.sleep(0.5)
    listener._check_query_timeout()

    assert not listener.state_manager.is_hot_window_active()
    assert face_states[-1] is AssistantState.THINKING, face_states[-4:]

    engine.release("what's the weather")
    assert _wait_until(lambda: len(_spoken(tts)) == 2)
    _spoken(tts)[1][1]()  # the last reply finishes
    assert _wait_until(listener.state_manager.is_hot_window_active)
    listener.stop()


def test_background_speech_after_an_earlier_reply_is_not_taken_as_a_follow_up(
        face_states, engine, no_fast_path):
    listener, tts = _create_listener(echo_tolerance=0.05)
    first_finished = _first_reply_spoken_while_second_is_generated(listener, tts, engine)
    first_finished()
    time.sleep(0.15)

    _install_intent_judge(listener, _make_judgment(directed=False, confidence="high",
                                                   reasoning="talking to someone else"))
    _hear(listener, "can you pass me the salt", started_ago=0.1, ended_ago=0.05)

    assert not listener.state_manager.is_collecting()
    listener.stop()


def test_with_the_hot_window_off_an_earlier_reply_leaves_the_orb_thinking(face_states, engine, no_fast_path):
    listener, tts = _create_listener()
    listener.cfg.hot_window_enabled = False
    first_finished = _first_reply_spoken_while_second_is_generated(listener, tts, engine)

    first_finished()

    assert face_states[-1] is AssistantState.THINKING

    engine.release("what's the weather")
    assert _wait_until(lambda: len(_spoken(tts)) == 2)
    _spoken(tts)[1][1]()
    assert face_states[-1] is AssistantState.IDLE
    listener.stop()


def test_only_the_last_reply_handed_to_tts_arms_the_hot_window(face_states, engine, no_fast_path):
    listener, tts = _create_listener(echo_tolerance=0.05)
    listener._dispatch_query("first")
    listener._dispatch_query("second")
    assert _wait_until(lambda: len(_spoken(tts)) == 2)

    _spoken(tts)[0][1]()  # the first reply finishes; the second is queued in TTS behind it
    time.sleep(0.15)
    assert not listener.state_manager.is_hot_window_active()

    _spoken(tts)[1][1]()
    assert _wait_until(listener.state_manager.is_hot_window_active)
    listener.stop()


def test_a_reply_with_nothing_to_say_leaves_the_orb_while_an_earlier_reply_still_speaks(
        face_states, engine, no_fast_path):
    listener, tts = _create_listener(echo_tolerance=0.05)
    _ask(listener, engine, "tell me the news")
    assert _wait_until(lambda: len(_spoken(tts)) == 1)
    first_finished = _spoken(tts)[0][1]
    tts.is_speaking.return_value = True  # the news is still playing
    engine.replies["what's the weather"] = None

    _ask(listener, engine, "what's the weather")
    assert _wait_until(lambda: not listener._pending_replies)

    assert face_states[-1] is not AssistantState.IDLE, face_states[-4:]

    tts.is_speaking.return_value = False
    first_finished()  # the news ends: it is now the last reply
    assert _wait_until(listener.state_manager.is_hot_window_active)
    listener.stop()


def test_a_following_reply_with_nothing_to_say_returns_the_orb_to_idle(face_states, engine, no_fast_path):
    listener, tts = _create_listener()
    engine.replies["what's the weather"] = None
    first_finished = _first_reply_spoken_while_second_is_generated(listener, tts, engine)
    first_finished()

    engine.release("what's the weather")

    assert _wait_until(lambda: face_states[-1] is AssistantState.IDLE), face_states[-4:]
    listener.stop()
