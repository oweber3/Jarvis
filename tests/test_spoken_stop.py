"""Saying "stop" while Jarvis talks: it stops Jarvis, and Jarvis's own echo never does.

Covers the during-TTS stop path end to end: an utterance captured over
continuous echo is cut at the during-TTS cap so a stop is heard promptly, a
stop transcribed after playback ended still cancels what follows, and a reply
that merely contains a stop word does not interrupt itself.
"""

import threading
import time

import pytest

from jarvis.listening.state_manager import ListeningState
from tests.audio_harness import ListenerHarness, white_noise
from tests.test_hot_window_input import _create_listener

pytestmark = pytest.mark.unit

STOP_COMMANDS = ["stop", "quiet", "shush", "silence", "enough", "shut up"]
VOICED = 0.1  # well above the energy VAD threshold


def _speaking_listener(reply, *, speaking=True):
    listener, tts = _create_listener(tts_speaking=speaking, echo_tolerance=0.05)
    listener.cfg.stop_commands = STOP_COMMANDS
    tts.is_speaking.return_value = True
    listener.track_tts_start(reply)
    tts.is_speaking.return_value = speaking
    return listener, tts


def _hear_captured_during_tts(listener, text, *, started_ago=1.5, ended_ago=0.3):
    now = time.time()
    listener._process_transcript(
        text, 0.01, now - started_ago, now - ended_ago,
        captured_during_tts=True,
        captured_tts_start_time=listener.echo_detector._tts_start_time,
    )


# ---------------------------------------------------------------------------
# A stop said over continuous echo is heard within the during-TTS cap
# ---------------------------------------------------------------------------

def test_stop_said_over_continuous_echo_interrupts_within_the_tts_utterance_cap():
    reply = "Tomorrow will be mostly sunny with a top temperature of twenty one degrees."
    with ListenerHarness({"vad_enabled": False}, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start(reply)
        harness.play(white_noise(1.0, level=VOICED, seed=1))  # Jarvis's own voice keeps VAD voiced
        said_stop_at = harness.now
        harness.play(white_noise(0.6, level=VOICED, seed=2), text="stop")
        harness.play(white_noise(6.0, level=VOICED, seed=3))
        cap = harness.listener.cfg.tts_max_utterance_ms / 1000.0

    interrupted_at = harness.tts.time_of("interrupt")
    assert interrupted_at is not None, "the stop was never heard while echo kept the VAD voiced"
    assert interrupted_at - said_stop_at <= cap + 0.1


def test_utterances_during_tts_never_outgrow_the_cap_while_echo_continues():
    with ListenerHarness({"vad_enabled": False}, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start("A long reply that keeps playing for a while.")
        harness.play(white_noise(8.0, level=VOICED, seed=4))
        cap = harness.listener.cfg.tts_max_utterance_ms / 1000.0

    assert len(harness.whisper_calls) >= 2
    assert all(call.duration <= cap + 0.05 for call in harness.whisper_calls)


# ---------------------------------------------------------------------------
# A stop captured during TTS still stops once playback has ended
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    "Tomorrow will be mostly sunny with a top temperature of 21 degrees.",
    "Here is the story of the Apollo missions and how they stopped in 1972.",
    "The nearest bus stop is two hundred metres away.",
])
@pytest.mark.parametrize("heard", ["stop", "stop.", "jarvis stop"])
def test_stop_transcribed_after_playback_ended_cancels_what_follows(reply, heard, capsys):
    listener, tts = _speaking_listener(reply, speaking=False)
    queued_reply = threading.Event()
    listener._pending_replies = (queued_reply,)
    listener.activate_hot_window()  # playback finished while Whisper was busy

    _hear_captured_during_tts(listener, heard)

    assert queued_reply.is_set(), "the next queued reply was not cancelled"
    time.sleep(0.15)  # past the echo tolerance that would open the follow-up window
    assert listener.state_manager.get_state() == ListeningState.WAKE_WORD
    assert not listener.state_manager.is_collecting()
    assert "Heard (echo)" not in capsys.readouterr().out
    listener.state_manager.stop()


def test_stop_after_the_follow_up_window_opened_closes_it():
    listener, _tts = _speaking_listener("It is sunny with a top of 21 degrees.", speaking=False)
    listener.activate_hot_window()
    time.sleep(0.15)
    assert listener.state_manager.is_hot_window_active()

    _hear_captured_during_tts(listener, "stop")

    assert listener.state_manager.get_state() == ListeningState.WAKE_WORD
    listener.state_manager.stop()


def test_stop_while_still_speaking_interrupts_and_drops_queued_replies():
    listener, tts = _speaking_listener("Tomorrow will be mostly sunny with a top temperature of 21 degrees.")
    queued_reply = threading.Event()
    listener._pending_replies = (queued_reply,)

    _hear_captured_during_tts(listener, "stop")

    tts.interrupt.assert_called()
    assert queued_reply.is_set()
    listener.state_manager.stop()


# ---------------------------------------------------------------------------
# Jarvis's own echo containing a stop word does not stop it
# ---------------------------------------------------------------------------

ECHOES_WITH_STOP_WORDS = [
    ("The nearest bus stop is two hundred metres away.", "the nearest bus stop is two hundred metres away"),
    ("The nearest bus stop is two hundred metres away.", "bus stop is"),
    ("The library has a quiet study room on the second floor.", "the library has a quiet study room"),
    ("She stopped the car at the lights and waited for the rain to ease.", "she stopped the car at the lights"),
    ("That should be enough rice for four people.", "that should be enough rice for four people"),
]


@pytest.mark.parametrize("reply,echo", ECHOES_WITH_STOP_WORDS)
def test_echo_containing_a_stop_word_does_not_interrupt_jarvis(reply, echo):
    listener, tts = _speaking_listener(reply)
    queued_reply = threading.Event()
    listener._pending_replies = (queued_reply,)

    _hear_captured_during_tts(listener, echo, started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_not_called()
    assert not queued_reply.is_set()
    listener.state_manager.stop()


@pytest.mark.parametrize("reply,echo", ECHOES_WITH_STOP_WORDS)
def test_echo_containing_a_stop_word_transcribed_after_playback_cancels_nothing(reply, echo):
    listener, _tts = _speaking_listener(reply, speaking=False)
    queued_reply = threading.Event()
    listener._pending_replies = (queued_reply,)
    listener.activate_hot_window()

    _hear_captured_during_tts(listener, echo, started_ago=0.8, ended_ago=0.1)

    assert not queued_reply.is_set()
    listener.state_manager.stop()


@pytest.mark.parametrize("heard", ["stop", "stop.", "jarvis stop", "shut up", "stop it please"])
def test_a_short_stop_still_stops_a_reply_that_contains_a_stop_word(heard):
    listener, tts = _speaking_listener("The nearest bus stop is two hundred metres away, past the quiet park.")

    _hear_captured_during_tts(listener, heard, started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_called()
    listener.state_manager.stop()
