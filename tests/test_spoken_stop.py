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
from tests.audio_harness import ListenerHarness, silence, white_noise
from tests.test_hot_window_input import _create_listener, _install_intent_judge, _make_judgment

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

def _hard_cap_seconds(listener):
    """The longest an utterance can run during TTS: the cap plus its grace for a pause between words."""
    return (listener.cfg.tts_max_utterance_ms + listener._UTTERANCE_CAP_GRACE_MS) / 1000.0


def test_stop_said_over_continuous_echo_interrupts_within_the_tts_utterance_cap():
    reply = "Tomorrow will be mostly sunny with a top temperature of twenty one degrees."
    with ListenerHarness({"vad_enabled": False}, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start(reply)
        harness.play(white_noise(1.0, level=VOICED, seed=1))  # Jarvis's own voice keeps VAD voiced
        said_stop_at = harness.now
        harness.play(white_noise(0.6, level=VOICED, seed=2), text="stop")
        harness.play(white_noise(6.0, level=VOICED, seed=3))
        longest = _hard_cap_seconds(harness.listener)

    interrupted_at = harness.tts.time_of("interrupt")
    assert interrupted_at is not None, "the stop was never heard while echo kept the VAD voiced"
    assert interrupted_at - said_stop_at <= longest + 0.1


def test_utterances_during_tts_never_outgrow_the_cap_while_echo_continues():
    with ListenerHarness({"vad_enabled": False}, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start("A long reply that keeps playing for a while.")
        harness.play(white_noise(8.0, level=VOICED, seed=4))
        longest = _hard_cap_seconds(harness.listener)

    assert len(harness.whisper_calls) >= 2
    assert all(call.duration <= longest + 0.05 for call in harness.whisper_calls)


def test_a_stop_straddling_the_cap_is_heard_whole():
    with ListenerHarness({"vad_enabled": False}, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start("A long reply that keeps playing for a while.")
        cap = harness.listener.cfg.tts_max_utterance_ms / 1000.0
        harness.play(white_noise(cap - 0.12, level=VOICED, seed=5))
        harness.play(white_noise(0.25, level=VOICED, seed=6), text="stop")  # across the cap
        harness.play(white_noise(4.0, level=VOICED, seed=7))

    assert harness.tts.time_of("interrupt") is not None


def test_long_speech_is_cut_in_a_pause_between_words():
    word, gap = 0.4, 0.1  # pauses far shorter than the endpoint silence
    with ListenerHarness({"vad_enabled": False}, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start("A long reply that keeps playing for a while.")
        started = harness.now
        for index in range(16):
            harness.play(white_noise(word, level=VOICED, seed=10 + index))
            harness.play(silence(gap))

    cuts = [call.end_time - started for call in harness.whisper_calls]
    assert len(cuts) >= 2
    for cut in cuts:
        position = round(cut % (word + gap), 3)
        assert position == 0 or position > word, f"cut mid-word at {cut:.2f}s"


# ---------------------------------------------------------------------------
# A stop captured during TTS still stops once playback has ended
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply", [
    "Tomorrow will be mostly sunny with a top temperature of 21 degrees.",
    "Here is the story of the Apollo missions and how they stopped in 1972.",
    "It was a quietly confident performance from the whole team.",
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


LONG_REPLY_WITH_STOP_EARLY = (
    "The nearest bus stop is two hundred metres away, and from there the number twelve runs every "
    "ten minutes into the centre of town, where the museum, the library and the covered market are "
    "all within a short walk of each other along the river path past the old bridge and the park."
)


@pytest.mark.parametrize("heard", ["stop", "stop.", "jarvis stop", "shut up"])
def test_a_bare_stop_stops_a_reply_that_contains_the_word_elsewhere(heard):
    listener, tts = _speaking_listener(LONG_REPLY_WITH_STOP_EARLY)
    listener.echo_detector._tts_start_time -= 12.0  # Jarvis is well past "bus stop" by now

    _hear_captured_during_tts(listener, heard, started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_called()
    listener.state_manager.stop()


def test_a_bare_stop_word_heard_while_jarvis_says_it_is_echo():
    listener, tts = _speaking_listener(LONG_REPLY_WITH_STOP_EARLY)

    _hear_captured_during_tts(listener, "stop.", started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_not_called()
    listener.state_manager.stop()


def test_the_wake_word_makes_a_stop_count_even_while_jarvis_says_it():
    listener, tts = _speaking_listener(LONG_REPLY_WITH_STOP_EARLY)

    _hear_captured_during_tts(listener, "jarvis stop", started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_called()
    listener.state_manager.stop()


@pytest.mark.parametrize("heard", ["top", "stops", "quite"])
def test_words_that_only_sound_like_a_stop_do_not_stop_jarvis(heard):
    listener, tts = _speaking_listener("Here is the weather for the rest of the week in London.")

    _hear_captured_during_tts(listener, heard, started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_not_called()
    listener.state_manager.stop()


# ---------------------------------------------------------------------------
# A stop word with more to it is a request, not a bare stop
# ---------------------------------------------------------------------------

COMMANDS_WITH_STOP_WORDS = ["stop the music", "jarvis stop the music", "stop the timer", "please stop"]


@pytest.mark.parametrize("heard", COMMANDS_WITH_STOP_WORDS)
def test_a_command_holding_a_stop_word_goes_to_the_judge_while_jarvis_speaks(heard):
    listener, tts = _speaking_listener("Here is the weather for the rest of the week in London.")
    judge = _install_intent_judge(listener, _make_judgment(directed=False, confidence="low"))
    queued_reply = threading.Event()
    listener._pending_replies = (queued_reply,)

    _hear_captured_during_tts(listener, heard, started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_not_called()
    assert not queued_reply.is_set()
    assert judge.judge.called, "the utterance never reached the intent judge"
    listener.state_manager.stop()


@pytest.mark.parametrize("heard", COMMANDS_WITH_STOP_WORDS)
def test_a_command_holding_a_stop_word_after_playback_cancels_nothing(heard):
    listener, _tts = _speaking_listener("Here is the weather for the rest of the week in London.",
                                        speaking=False)
    queued_reply = threading.Event()
    listener._pending_replies = (queued_reply,)
    listener.activate_hot_window()

    _hear_captured_during_tts(listener, heard, started_ago=0.8, ended_ago=0.1)

    assert not queued_reply.is_set()
    if heard.startswith("jarvis"):
        assert listener.state_manager.get_pending_query() == heard.removeprefix("jarvis ")
    else:
        time.sleep(0.15)
        assert listener.state_manager.is_hot_window_active()
    listener.state_manager.stop()


@pytest.mark.parametrize("heard", ["jarvis stop the music", "jarvis stop the timer"])
def test_an_addressed_command_holding_a_stop_word_does_not_cancel_the_reply_being_generated(heard):
    listener, _tts = _create_listener()
    generating = threading.Event()
    listener._pending_replies = (generating,)
    now = time.time()

    listener._process_transcript(heard, 0.01, now - 1.0, now - 0.5,
                                 captured_during_tts=False, captured_tts_start_time=0.0)

    assert not generating.is_set()
    listener.state_manager.stop()


def test_the_judge_can_still_hear_a_polite_stop_while_jarvis_speaks():
    listener, tts = _speaking_listener("Here is the weather for the rest of the week in London.")
    _install_intent_judge(listener, _make_judgment(directed=True, stop=True))

    _hear_captured_during_tts(listener, "please stop", started_ago=0.8, ended_ago=0.1)

    tts.interrupt.assert_called()
    listener.state_manager.stop()
