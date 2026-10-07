"""The listener keeps hearing: an empty setting or one failing transcript never costs voice input."""

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_hot_window_input import _create_listener, _install_intent_judge, _make_judgment

pytestmark = pytest.mark.unit

REPLY = "Here is a long answer about the weather in London today and tomorrow, with sunshine and showers."


def _result(text, *, captured_during_tts=False, captured_tts_start_time=0.0):
    now = time.time()
    return SimpleNamespace(
        text=text, language="en", low_confidence_events=(), start_time=now - 1.0,
        end_time=now - 0.2, speech_end_time=now - 0.2, energy=0.01, dictation_generation=0,
        captured_during_tts=captured_during_tts, captured_tts_start_time=captured_tts_start_time,
        speaker_verdict=None,
    )


# ---------------------------------------------------------------------------
# A speech rate left empty in Settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heard", [
    "here is a long answer about the weather in london today",  # Jarvis's own echo
    "what about paris then please",                             # the user talking over it
])
def test_an_empty_speech_rate_does_not_break_speech_heard_during_tts(heard):
    listener, tts = _create_listener(tts_speaking=True)
    listener.cfg.tts_rate = None
    listener.track_tts_start(REPLY)
    now = time.time()

    listener._process_transcript(
        heard, 0.01, now - 1.0, now - 0.2, captured_during_tts=True,
        captured_tts_start_time=listener.echo_detector._tts_start_time)

    tts.interrupt.assert_not_called()
    assert not listener.state_manager.is_collecting()
    listener.state_manager.stop()


def test_an_empty_speech_rate_still_salvages_a_follow_up_after_tts():
    listener, tts = _create_listener()
    listener.cfg.tts_rate = None
    _install_intent_judge(listener, _make_judgment(query="what about paris tomorrow then"))
    tts.is_speaking.return_value = True
    listener.track_tts_start(REPLY)
    tts.is_speaking.return_value = False
    listener.activate_hot_window()
    now = time.time()

    listener._process_transcript(
        "with sunshine and showers what about paris tomorrow then", 0.01, now + 0.05, now + 1.5,
        captured_during_tts=True, captured_tts_start_time=listener.echo_detector._tts_start_time)

    assert "paris" in listener.state_manager.get_pending_query()
    listener.state_manager.stop()


# ---------------------------------------------------------------------------
# One transcript that fails to process
# ---------------------------------------------------------------------------

def test_a_transcript_that_fails_to_process_does_not_stop_the_next_one():
    listener, _tts = _create_listener()
    real_process = listener._process_transcript
    calls = []

    def flaky(text, *args, **kwargs):
        calls.append(text)
        if len(calls) == 1:
            raise TypeError("unexpected value")
        return real_process(text, *args, **kwargs)

    with patch.object(listener, "_process_transcript", side_effect=flaky):
        listener._handle_transcription_result(_result("jarvis what time is it"))
        listener._handle_transcription_result(_result("jarvis what time is it"))

    assert calls == ["jarvis what time is it", "jarvis what time is it"]
    assert listener.state_manager.is_collecting()
    listener.state_manager.stop()
