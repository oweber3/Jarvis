"""An interrupted reply never reports that it finished, so a spoken "stop" cannot open the follow-up window."""

import threading
import time

import pytest

pytest.importorskip("sounddevice")

from tests.test_voice_latency import FakeVoice, fake_audio, make_tts  # noqa: F401  (fixture)


def _voice_seconds(seconds):
    return int(seconds * FakeVoice().config.sample_rate)


def test_interrupt_while_playing_stops_without_completing(fake_audio):
    completed = []
    synthesised = threading.Event()
    tts = make_tts(FakeVoice(samples_per_sentence=_voice_seconds(30)))
    tts.start()
    try:
        tts.speak("One. Two.", completion_callback=lambda: completed.append(True),
                  duration_callback=lambda _seconds: synthesised.set())
        assert synthesised.wait(3)  # the whole reply is synthesised and playing
        assert tts.is_speaking()
        tts.interrupt()
        deadline = time.monotonic() + 3
        while tts.is_speaking() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not tts.is_speaking()
        assert completed == []
    finally:
        tts.stop()


def test_interrupt_drops_replies_queued_behind_the_one_playing(fake_audio):
    # "Stop" silences Jarvis: a reply already handed to TTS must not start afterwards.
    started, completed = [], []
    synthesised = threading.Event()
    tts = make_tts(FakeVoice(samples_per_sentence=_voice_seconds(30)))
    tts.start()
    try:
        tts.speak("First reply.", duration_callback=lambda _seconds: synthesised.set())
        tts.speak("Second reply.", first_audio_callback=lambda: started.append("second"),
                  completion_callback=lambda: completed.append("second"))
        assert synthesised.wait(3)
        tts.interrupt()
        time.sleep(0.8)
        assert started == [] and completed == []

        after = threading.Event()
        tts.speak("A new reply.", completion_callback=after.set)
        assert after.wait(5), "speech after a stop no longer plays"
    finally:
        tts.stop()


def test_a_reply_played_to_the_end_still_completes(fake_audio):
    completed = threading.Event()
    tts = make_tts(FakeVoice())
    tts.start()
    try:
        tts.speak("One. Two.", completion_callback=completed.set)
        assert completed.wait(3)
    finally:
        tts.stop()
