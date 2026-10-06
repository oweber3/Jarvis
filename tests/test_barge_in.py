"""Talking over Jarvis: the owner's voice ducks TTS before Whisper has heard a word."""

import numpy as np
import pytest

from tests.audio_harness import ListenerHarness, fixture, pad, silence
from tests.audio_harness.stubs import StubVerifier

pytestmark = pytest.mark.unit

LEAD_IN = 0.4  # seconds of silence before the speaker starts
JARVIS_REPLY = "The time is ten past four in the afternoon, and it is raining."


def _onset(clip_audio, start: float) -> float:
    """Simulated time at which the voice in ``clip_audio`` begins."""
    voiced = np.nonzero(np.abs(clip_audio) > 0.05 * np.abs(clip_audio).max())[0]
    return start + float(voiced[0]) / 16000.0


def _speak_over_tts(harness, clip, *, text):
    harness.tts.speaking = True
    harness.listener.track_tts_start(JARVIS_REPLY)
    start = harness.now
    audio = pad(clip.audio, before=LEAD_IN, after=0.2)
    harness.play(audio, text=text)
    onset = _onset(audio, start)
    harness.play(silence(2.0))
    return onset


def test_the_owner_talking_over_tts_ducks_it_within_the_verification_window():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    verifier = StubVerifier(score=0.9)
    with ListenerHarness(speaker_verifier=verifier, with_tts=True) as harness:
        onset = _speak_over_tts(harness, clip, text="")
        ducked_at = harness.tts.time_of("duck")

    assert ducked_at is not None, "TTS was never ducked"
    verify_s = harness.listener.cfg.barge_in_verify_ms / 1000.0
    assert 0 <= ducked_at - onset <= verify_s + 0.3
    # It ducked on a short window of voiced audio, not the whole utterance.
    assert verifier.windows and verifier.windows[0] <= verify_s + 0.5


def test_a_voice_that_is_not_the_owner_never_ducks_tts():
    clip = fixture("wake_command__jarvis_what_time_is_it__ryan.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.05), with_tts=True) as harness:
        _speak_over_tts(harness, clip, text="")
    assert harness.tts.time_of("duck") is None
    assert harness.tts.time_of("interrupt") is None


def test_a_stop_command_after_a_barge_in_stops_the_speech():
    clip = fixture("wake__jarvis__alan.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.9), with_tts=True) as harness:
        _speak_over_tts(harness, clip, text="stop")
    assert harness.tts.time_of("duck") is not None
    assert harness.tts.time_of("interrupt") is not None
    assert harness.tts.time_of("unduck") is None


def test_a_new_request_after_a_barge_in_replaces_the_speech():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.9), with_tts=True) as harness:
        _speak_over_tts(harness, clip, text=clip.text)
    assert harness.tts.time_of("duck") is not None
    assert harness.tts.time_of("interrupt") is not None


def test_chatter_after_a_barge_in_resumes_the_speech_at_full_volume():
    clip = fixture("speech__i_think_we_should_leave_for_the_station_in_about_ten_minutes__alan.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.9), with_tts=True) as harness:
        _speak_over_tts(harness, clip, text=clip.text)
    assert harness.tts.time_of("duck") is not None
    assert harness.tts.time_of("unduck") is not None
    assert harness.tts.time_of("interrupt") is None
    assert not harness.tts.ducked


def test_an_empty_transcript_after_a_barge_in_resumes_the_speech():
    clip = fixture("speech__can_you_pass_me_the_salt_and_the_pepper_please__lessac.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.9), with_tts=True) as harness:
        _speak_over_tts(harness, clip, text="")
    assert harness.tts.time_of("duck") is not None
    assert not harness.tts.ducked


def test_speech_when_jarvis_is_silent_does_not_barge_in():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    verifier = StubVerifier(score=0.9)
    with ListenerHarness(speaker_verifier=verifier, with_tts=True) as harness:
        harness.play(pad(clip.audio, before=LEAD_IN), text=clip.text)
        harness.play(silence(2.0))
    assert harness.tts.events == []
    assert verifier.score_calls == 0


def test_barge_in_needs_an_enrolled_voice():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness(with_tts=True) as harness:
        _speak_over_tts(harness, clip, text="")
    assert harness.tts.time_of("duck") is None


def test_barge_in_can_be_switched_off():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    verifier = StubVerifier(score=0.9)
    with ListenerHarness({"barge_in_enabled": False}, speaker_verifier=verifier, with_tts=True) as harness:
        _speak_over_tts(harness, clip, text="")
    assert harness.tts.time_of("duck") is None
    assert verifier.score_calls == 0


def test_a_failed_verification_is_retried_as_more_speech_arrives():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    scores = iter([0.1, 0.1, 0.9] + [0.9] * 20)
    verifier = StubVerifier(score=lambda audio: next(scores))
    with ListenerHarness(speaker_verifier=verifier, with_tts=True) as harness:
        onset = _speak_over_tts(harness, clip, text="")
        ducked_at = harness.tts.time_of("duck")
    assert ducked_at is not None
    assert verifier.score_calls >= 3
    assert ducked_at - onset > harness.listener.cfg.barge_in_verify_ms / 1000.0


def test_it_ducks_once_per_reply_even_when_speech_continues():
    clip = fixture("wake_command__jarvis_open_word__alan.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.9), with_tts=True) as harness:
        _speak_over_tts(harness, clip, text="")
    assert [e for _, e in harness.tts.events].count("duck") == 1


def test_one_lucky_window_is_not_enough_to_duck():
    clip = fixture("wake_command__jarvis_open_word__alan.wav")
    flicker = iter([0.9, 0.05] * 100)
    verifier = StubVerifier(score=lambda audio: next(flicker))
    with ListenerHarness(speaker_verifier=verifier, with_tts=True) as harness:
        _speak_over_tts(harness, clip, text="")
    assert verifier.score_calls >= 4
    assert harness.tts.time_of("duck") is None


def _duck_then_end_reply(harness, clip):
    """The owner talks during the last words; the reply ends on its own right after the duck."""
    harness.tts.speaking = True
    harness.listener.track_tts_start(JARVIS_REPLY)
    audio = pad(clip.audio, before=LEAD_IN, after=0.2)
    block = harness.block_samples
    for offset in range(0, len(audio), block):
        harness.clock.advance(block / 16000)
        harness.listener._process_audio_block(audio[offset:offset + block].reshape(-1, 1))
        if harness.tts.ducked:
            harness.tts.speaking = False
        harness._drain()


def test_a_reply_that_ends_while_ducked_leaves_the_next_reply_at_full_volume():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.9), with_tts=True) as harness:
        _duck_then_end_reply(harness, clip)
        harness.play(silence(2.0))
        assert harness.tts.time_of("duck") is not None
        harness.tts.speaking = True
        harness.listener.track_tts_start("The next reply.")
        harness.play(silence(1.0))
        assert not harness.tts.ducked


def test_a_queued_reply_that_starts_while_ducked_plays_at_full_volume():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(score=0.9), with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start(JARVIS_REPLY)
        audio = pad(clip.audio, before=LEAD_IN, after=0.2)
        block = harness.block_samples
        for offset in range(0, len(audio), block):
            harness.clock.advance(block / 16000)
            harness.listener._process_audio_block(audio[offset:offset + block].reshape(-1, 1))
            harness._drain()
            if harness.tts.ducked:
                break
        assert harness.tts.ducked
        harness.listener.track_tts_start("A queued reply.")
        assert not harness.tts.ducked
