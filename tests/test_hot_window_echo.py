"""Hot-window echo rejection only discards speech that could contain Jarvis's own voice.

Echo is acoustic: it exists only while a reply plays and for ``echo_tolerance`` after it ends.
A follow-up that starts later cannot be echo, even when it repeats words of the reply, so it
must reach the pending confirmation, the fast path or the intent judge. Genuine echo (captured
while the reply played or straight after it) is still rejected.
"""

import time
from unittest.mock import patch

import pytest

from tests.test_hot_window_input import (
    _create_listener,
    _install_intent_judge,
    _make_judgment,
    _wait_for_hot_window_active,
)

pytestmark = pytest.mark.unit

WEATHER = ("It is 14 degrees and sunny in London right now. Tomorrow will be cloudy with light rain "
           "in the afternoon.")
QUESTION = "I need your confirmation to delete file report.docx. Say yes or no."


@pytest.fixture
def no_fast_path():
    with patch("jarvis.fastpath.dispatcher.match_command", return_value=None) as fast_path:
        yield fast_path


def _hear(listener, text, *, started_ago, ended_ago, during_tts=False):
    """Process a transcript as the listener loop does, in the TTS context of its capture."""
    now = time.time()
    listener._transcript_buffer.add(text, now - started_ago, now - ended_ago, 0.01, during_tts)
    listener._process_transcript(
        text, 0.01, now - started_ago, now - ended_ago,
        captured_during_tts=during_tts,
        captured_tts_start_time=listener.echo_detector._tts_start_time,
    )


def _jarvis_said(listener, reply, *, playback=0.05):
    """Jarvis spoke ``reply``; playback finished and the follow-up window opened."""
    listener.track_tts_start(reply)
    time.sleep(playback)
    listener.activate_hot_window()
    assert _wait_for_hot_window_active(listener), "hot window never opened"


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


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_an_answer_in_the_hot_window_reaches_the_pending_confirmation(monkeypatch, answer):
    listener, _tts = _create_listener()
    store = FakeConfirmationStore()
    monkeypatch.setattr("jarvis.tools.confirmation.get_confirmation_store", lambda: store)
    _jarvis_said(listener, QUESTION)
    time.sleep(0.4)  # the user answers a moment after Jarvis stopped talking

    _hear(listener, answer, started_ago=0.3, ended_ago=0.1)

    assert store.answers == [answer]
    listener.state_manager.stop()


@pytest.mark.parametrize("reply, follow_up", [
    (WEATHER, "and tomorrow?"),
    ("I've set the timer for ten minutes.", "jarvis set another timer for five minutes"),
])
def test_a_follow_up_that_repeats_words_of_the_reply_is_heard(no_fast_path, reply, follow_up):
    listener, _tts = _create_listener()
    _install_intent_judge(listener, _make_judgment(query=follow_up))
    _jarvis_said(listener, reply)
    time.sleep(0.4)

    _hear(listener, follow_up, started_ago=0.3, ended_ago=0.1)

    assert listener.state_manager.get_pending_query() == follow_up
    listener.state_manager.stop()


def test_echo_captured_while_the_reply_played_is_still_rejected(no_fast_path):
    listener, _tts = _create_listener()
    judge = _install_intent_judge(listener, _make_judgment(query="tomorrow will be cloudy"))
    _jarvis_said(listener, WEATHER, playback=0.3)

    # Whisper finished the tail of the reply only after playback ended.
    _hear(listener, "tomorrow will be cloudy with light rain in the afternoon",
          started_ago=0.6, ended_ago=0.35, during_tts=True)

    assert not listener.state_manager.is_collecting()
    assert not judge.judge.called
    listener.state_manager.stop()


def test_echo_starting_just_after_playback_ended_is_still_rejected(no_fast_path):
    listener, _tts = _create_listener()
    _install_intent_judge(listener, _make_judgment(query="in the afternoon"))
    _jarvis_said(listener, WEATHER)

    # The speaker's last words reach the microphone within echo_tolerance of the end.
    _hear(listener, "light rain in the afternoon", started_ago=0.15, ended_ago=0.0)

    assert not listener.state_manager.is_collecting()
    listener.state_manager.stop()


def test_echo_is_still_rejected_when_the_end_of_the_reply_is_unknown(no_fast_path):
    """Without a recorded end of the reply, its own words in the hot window stay echo."""
    listener, _tts = _create_listener()
    _install_intent_judge(listener, _make_judgment(query="what time is it"))
    listener.track_tts_start("What time is it? It is ten past four.")
    listener.state_manager.schedule_hot_window_activation()  # no finish time recorded
    assert _wait_for_hot_window_active(listener)

    _hear(listener, "what time is it? it is ten past four.", started_ago=0.2, ended_ago=0.1)

    assert not listener.state_manager.is_collecting()
    listener.state_manager.stop()


def test_an_answer_captured_by_the_microphone_reaches_the_pending_confirmation(monkeypatch):
    """Real capture backdates an utterance's start by the VAD pre-roll. A "yes" whose voice
    starts after the echo window must not count as echo because its pre-roll reaches into it."""
    from tests.test_audio_capture_backlog import QUIET, VOICED, _drain_capture, _listener_with_audio, _queue_audio

    listener = _listener_with_audio()
    store = FakeConfirmationStore()
    monkeypatch.setattr("jarvis.tools.confirmation.get_confirmation_store", lambda: store)
    listener.track_tts_start(QUESTION)
    time.sleep(0.05)
    listener.activate_hot_window()
    finished = listener.echo_detector._last_tts_finish_time
    tolerance = listener.echo_detector.echo_tolerance

    # 240 ms of quiet (the pre-roll), then "yes" heard about 0.4 s after the question ended.
    time.sleep(max(0.0, finished + 1.58 - time.time()))
    _queue_audio(listener, (0.24, QUIET), (0.4, VOICED), (0.8, QUIET))
    _drain_capture(listener)
    job = listener._transcription_jobs_q.get_nowait()
    assert job.start_time < finished + tolerance  # the backdated start falls in the echo window

    with patch.object(listener, "_transcribe_audio", return_value=("yes", "en", ())):
        listener._transcribe_job(job)
    listener._transcription_jobs_q.task_done()
    listener._handle_transcription_result(listener._transcription_results_q.get_nowait())

    assert store.answers == ["yes"]
    listener.state_manager.stop()
