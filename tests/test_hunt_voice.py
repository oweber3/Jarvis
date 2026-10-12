"""Reproductions of voice listener defects (bug hunt).

Each test drives the real VoiceListener, StateManager and EchoDetector with fakes for
Whisper, audio, TTS, the intent judge and the reply engine, and states the behaviour the
spec promises. They fail on the current code.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from jarvis import assistant_state
from jarvis.assistant_state import AssistantState
from tests.test_hot_window_input import (
    _create_listener,
    _install_intent_judge,
    _make_judgment,
    _wait_for_hot_window_active,
)

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


class BlockingEngine:
    """Stands in for run_reply_engine: records each call and waits until released."""

    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.started = threading.Event()

    def __call__(self, db, cfg, tts, query, dialogue_memory, language=None, addressed=True, **_):
        self.calls.append((query, addressed))
        self.started.set()
        assert self.release.wait(5), "engine never released"
        return f"reply to {query}"


@pytest.fixture
def engine(monkeypatch):
    from jarvis.reply import engine as engine_module
    fake = BlockingEngine()
    monkeypatch.setattr(engine_module, "run_reply_engine", fake)
    yield fake
    fake.release.set()


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _hear(listener, text, *, started_ago=1.0, ended_ago=0.5, during_tts=False):
    """Process a transcript as the listener loop does, with the TTS context of its capture."""
    now = time.time()
    listener._transcript_buffer.add(text, now - started_ago, now - ended_ago, 0.01, during_tts)
    listener._process_transcript(
        text, 0.01, now - started_ago, now - ended_ago,
        captured_during_tts=during_tts,
        captured_tts_start_time=listener.echo_detector._tts_start_time,
    )


def _jarvis_said(listener, reply):
    """Jarvis spoke ``reply``; it finished and the follow-up window opened."""
    listener.track_tts_start(reply)
    time.sleep(0.05)
    listener.activate_hot_window()
    assert _wait_for_hot_window_active(listener), "hot window never opened"


def _dispatch_first_request(listener, engine, face_states):
    """Ask Jarvis something and leave its reply being generated."""
    _install_intent_judge(listener, _make_judgment(query="what's the weather"))
    listener.state_manager.voice_collect_seconds = 0.05
    _hear(listener, "jarvis what's the weather", started_ago=0.6, ended_ago=0.1)
    assert listener.state_manager.is_collecting()
    time.sleep(0.1)
    listener._check_query_timeout()
    assert engine.started.wait(2), "the first request was never dispatched"
    assert face_states[-1] is AssistantState.THINKING


class FakeConfirmationStore:
    """A pending voice confirmation that records the answers it is given."""

    def __init__(self):
        self.answers = []

    def has_pending_voice(self):
        return True

    def handle_voice_response(self, answer, language="en"):
        from jarvis.tools.confirmation import VoiceResponseStatus
        self.answers.append(answer)
        return VoiceResponseStatus.NEGATIVE

    def clear_pending(self):
        pass


# ---------------------------------------------------------------------------
# Bug 1: the hot window's early echo check discards real follow-ups
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["yes", "no"])
def test_a_spoken_answer_in_the_hot_window_reaches_the_pending_confirmation(monkeypatch, answer):
    """Jarvis asks "... Say yes or no." and opens the hot window for the answer. The answer,
    said a second after Jarvis stopped talking, must reach the confirmation store."""
    listener, _tts = _create_listener()
    store = FakeConfirmationStore()
    monkeypatch.setattr("jarvis.tools.confirmation.get_confirmation_store", lambda: store)
    _jarvis_said(listener, "I need your confirmation to delete file report.docx. Say yes or no.")

    _hear(listener, answer, started_ago=0.3, ended_ago=0.1)

    assert store.answers == [answer]
    listener.state_manager.stop()


@pytest.mark.parametrize("reply, follow_up", [
    ("It is 14 degrees and sunny in London right now. Tomorrow will be cloudy with light rain "
     "in the afternoon.", "and tomorrow?"),
    ("I've set the timer for ten minutes.", "jarvis set another timer for five minutes"),
])
def test_a_follow_up_that_reuses_words_of_the_reply_is_not_rejected_as_echo(no_fast_path, reply, follow_up):
    """Speech that starts well after Jarvis stopped talking cannot be its echo; sharing a word
    or two with the reply must not get a follow-up (even one with the wake word) discarded."""
    listener, _tts = _create_listener()
    _install_intent_judge(listener, _make_judgment(query=follow_up))
    _jarvis_said(listener, reply)

    _hear(listener, follow_up, started_ago=0.6, ended_ago=0.1)

    assert listener.state_manager.is_collecting(), "the follow-up was discarded as echo"
    listener.state_manager.stop()


# ---------------------------------------------------------------------------
# Bug 2: a wake-word request heard while a reply is being generated is
# treated as unaddressed (no intent judge, no fast path, addressed=False)
# ---------------------------------------------------------------------------

def test_a_wake_word_request_while_a_reply_is_generated_is_passed_on_as_addressed(
        face_states, engine, no_fast_path):
    listener, _tts = _create_listener()
    _dispatch_first_request(listener, engine, face_states)

    # While the first reply is still being generated, the user asks something else.
    _install_intent_judge(listener, _make_judgment(query="paste"))
    _hear(listener, "jarvis paste", started_ago=0.6, ended_ago=0.1)
    time.sleep(0.1)
    listener._check_query_timeout()
    engine.release.set()

    assert _wait_until(lambda: len(engine.calls) == 2), engine.calls
    assert engine.calls[1] == ("paste", True)
    listener.stop()


def test_a_wake_word_request_while_a_reply_is_generated_is_judged_like_any_other(
        face_states, engine, no_fast_path):
    listener, _tts = _create_listener()
    _dispatch_first_request(listener, engine, face_states)

    judge = _install_intent_judge(listener, _make_judgment(query="what is the capital of france"))
    _hear(listener, "um jarvis what's the capital of france", started_ago=0.6, ended_ago=0.1)

    assert judge.judge.called, "the wake-word utterance never reached the intent judge"
    assert listener.state_manager.get_pending_query() == "what is the capital of france"
    listener.stop()


# ---------------------------------------------------------------------------
# Bug 3: background speech while a reply is generated resets the orb to idle
# ---------------------------------------------------------------------------

def test_background_speech_while_a_reply_is_generated_keeps_the_orb_thinking(
        face_states, engine, no_fast_path):
    listener, _tts = _create_listener()
    _dispatch_first_request(listener, engine, face_states)

    _hear(listener, "the kettle has boiled", started_ago=0.6, ended_ago=0.1)

    assert face_states[-1] is AssistantState.THINKING
    listener.stop()


# ---------------------------------------------------------------------------
# Bug 4: the follow-up window of an earlier reply opens while the next reply
# is still being generated
# ---------------------------------------------------------------------------

def test_the_end_of_an_earlier_reply_does_not_reset_the_orb_while_the_next_is_generated(
        face_states, engine, no_fast_path):
    """The user asks a new question over Jarvis's reply; the new reply is being generated when
    the earlier one finishes playing. The orb must keep showing that Jarvis is working."""
    listener, _tts = _create_listener(hot_window_seconds=0.3, echo_tolerance=0.05)
    listener.track_tts_start("Here is the news for today.")
    _dispatch_first_request(listener, engine, face_states)

    listener.activate_hot_window()  # the earlier reply finishes playing
    time.sleep(0.6)  # past echo tolerance and the whole hot window
    listener._check_query_timeout()

    assert face_states[-1] is AssistantState.THINKING, face_states[-4:]
    listener.stop()


def test_background_speech_after_an_earlier_reply_is_not_queued_as_a_request(
        face_states, engine, no_fast_path):
    """While the next reply is being generated, nobody is following up the earlier reply;
    chatter the judge rejects must not be collected as a new request."""
    listener, _tts = _create_listener(echo_tolerance=0.05)
    listener.track_tts_start("Here is the news for today.")
    _dispatch_first_request(listener, engine, face_states)
    listener.activate_hot_window()  # the earlier reply finishes playing
    time.sleep(0.15)

    _install_intent_judge(listener, _make_judgment(directed=False, confidence="high",
                                                   reasoning="talking to someone else"))
    _hear(listener, "can you pass me the salt", started_ago=0.1, ended_ago=0.05)

    assert not listener.state_manager.is_collecting(), "chatter was collected as a request"
    listener.stop()


# ---------------------------------------------------------------------------
# Bug 5: after one failed judge request, hot-window follow-ups are ignored for
# 30 s (spec: when the judge is unavailable the hot window override still applies)
# ---------------------------------------------------------------------------

def _unreachable_judge(listener, monkeypatch):
    """A real intent judge whose LLM server refuses connections."""
    import requests
    from jarvis.listening.intent_judge import IntentJudge, IntentJudgeConfig
    backend = MagicMock()
    backend.chat.side_effect = requests.exceptions.ConnectionError("connection refused")
    monkeypatch.setattr("jarvis.listening.intent_judge.get_llm_backend", lambda cfg: backend)
    listener._intent_judge = IntentJudge(IntentJudgeConfig(cfg=listener.cfg, timeout_sec=1.0))
    return backend


def test_hot_window_follow_ups_are_accepted_while_the_judge_server_is_unreachable(
        no_fast_path, monkeypatch):
    listener, _tts = _create_listener()
    _unreachable_judge(listener, monkeypatch)

    # First exchange: the follow-up is accepted by the hot-window fallback.
    _jarvis_said(listener, "It is sunny in London.")
    _hear(listener, "what about paris", started_ago=0.6, ended_ago=0.1)
    assert listener.state_manager.get_pending_query() == "what about paris"
    listener.state_manager.clear_collection()   # dispatched ...
    listener._end_engagement()                  # ... and its reply starts playing

    # Second exchange, a few seconds later: the same kind of follow-up.
    _jarvis_said(listener, "Paris is cloudy.")
    _hear(listener, "and in berlin", started_ago=0.6, ended_ago=0.1)

    assert listener.state_manager.get_pending_query() == "and in berlin", \
        "the hot-window follow-up was ignored"
    listener.state_manager.stop()


# ---------------------------------------------------------------------------
# Bug 6: a judge answer with "query": null becomes the request "None"
# ---------------------------------------------------------------------------

def test_a_directed_judgment_with_a_null_query_does_not_become_the_request_none(
        no_fast_path, monkeypatch):
    listener, _tts = _create_listener()
    backend = MagicMock()
    backend.chat.return_value = {"message": {"content": (
        '{"directed": true, "query": null, "stop": false, "confidence": "high", '
        '"reasoning": "wake word, greeting"}')}}
    monkeypatch.setattr("jarvis.listening.intent_judge.get_llm_backend", lambda cfg: backend)
    from jarvis.listening.intent_judge import IntentJudge, IntentJudgeConfig
    listener._intent_judge = IntentJudge(IntentJudgeConfig(cfg=listener.cfg))

    _hear(listener, "jarvis can you hear me", started_ago=0.6, ended_ago=0.1)

    assert listener.state_manager.is_collecting()
    assert listener.state_manager.get_pending_query().lower() != "none"
    listener.state_manager.stop()


# ---------------------------------------------------------------------------
# Controls: the same harness passes where the code behaves, so the failures
# above come from the defects, not from the fakes.
# ---------------------------------------------------------------------------

def test_control_unrelated_follow_up(no_fast_path):
    listener, _tts = _create_listener()
    _install_intent_judge(listener, _make_judgment(query="what about paris"))
    _jarvis_said(listener, "It is 14 degrees and sunny in London right now.")
    _hear(listener, "what about paris", started_ago=0.6, ended_ago=0.1)
    assert listener.state_manager.is_collecting()
    listener.state_manager.stop()


def test_control_confirmation_with_wake_word_outside_hot_window(monkeypatch):
    listener, _tts = _create_listener()
    store = FakeConfirmationStore()
    monkeypatch.setattr("jarvis.tools.confirmation.get_confirmation_store", lambda: store)
    _hear(listener, "jarvis yes", started_ago=0.3, ended_ago=0.1)
    assert store.answers == ["yes"]
    listener.state_manager.stop()


def test_control_wake_word_request_when_idle_is_addressed(face_states, engine, no_fast_path):
    listener, _tts = _create_listener()
    _dispatch_first_request(listener, engine, face_states)
    engine.release.set()
    assert _wait_until(lambda: len(engine.calls) == 1)
    assert engine.calls[0] == ("what's the weather", True)
    listener.stop()
