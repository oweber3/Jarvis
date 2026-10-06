"""The WAV harness drives the real listener frame loop without a microphone."""

import numpy as np
import pytest

from tests.audio_harness import (
    ListenerHarness,
    fixture,
    fixtures_of_kind,
    music_bed,
    pad,
    silence,
)

pytestmark = pytest.mark.unit


def test_wake_command_reaches_whisper_in_full():
    clip = fixture("wake_command__jarvis_open_word__alan.wav")
    with ListenerHarness() as harness:
        harness.play(clip.audio, text=clip.text)
        harness.play(silence(1.5))

    assert len(harness.whisper_calls) == 1
    call = harness.whisper_calls[0]
    # The whole utterance, wake word included, is what Whisper receives.
    assert call.duration >= clip.duration - 0.3
    assert call.text == clip.text


def test_transcript_mode_transcribes_ambient_speech():
    clips = fixtures_of_kind("speech")
    with ListenerHarness() as harness:
        for clip in clips:
            harness.play(clip.audio, text=clip.text)
            harness.play(silence(1.5))

    assert [call.text for call in harness.whisper_calls] == [clip.text for clip in clips]
    assert harness.collections == []


def test_wake_worded_request_is_dispatched():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness() as harness:
        harness.play(clip.audio, text=clip.text)
        harness.play(silence(3.0))

    # A routine request goes straight out (fast path) or through a collection.
    assert harness.dispatched and "time" in harness.dispatched[0].lower()


def test_audio_at_other_capture_rates_is_resampled_for_whisper():
    clip = fixture("wake__jarvis__lessac.wav")
    with ListenerHarness(capture_rate=48000) as harness:
        harness.play(clip.audio, text=clip.text)
        harness.play(silence(1.5))

    assert len(harness.whisper_calls) == 1
    assert harness.whisper_calls[0].duration == pytest.approx(clip.duration, abs=0.4)


def test_simulated_clock_follows_the_audio():
    with ListenerHarness() as harness:
        start = harness.now
        harness.play(silence(2.0))
        assert harness.now - start == pytest.approx(2.0, abs=0.03)


def test_beds_mix_under_speech():
    clip = fixture("wake__jarvis__ryan.wav")
    bed = music_bed(3.0, level=0.02, seed=1)
    with ListenerHarness() as harness:
        harness.play(pad(clip.audio, before=0.5, after=1.5), text=clip.text, bed=bed, snr_db=15)
        harness.play(silence(1.5))

    assert harness.whisper_calls
    assert any(call.text == clip.text for call in harness.whisper_calls)
    assert np.isfinite(harness.whisper_calls[0].audio).all()


def test_bare_wake_word_waits_for_the_request_in_the_next_breath():
    wake = fixture("wake__jarvis__alba.wav")
    request = fixture("speech__can_you_pass_me_the_salt_and_the_pepper_please__lessac.wav")
    with ListenerHarness() as harness:
        harness.play(wake.audio, text=wake.text)
        harness.play(silence(1.0))
        harness.play(request.audio, text=request.text)
        harness.play(silence(3.0))

    assert harness.collections == [""]
    assert len(harness.dispatched) == 1
    assert "salt" in harness.dispatched[0].lower()


def test_harness_records_face_states_and_never_reads_the_user_config(monkeypatch, tmp_path):
    import os

    user_config = tmp_path / "user-config.json"
    user_config.write_text('{"wake_word": "computer"}', encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(user_config))
    clip = fixture("wake__jarvis__alba.wav")
    with ListenerHarness() as harness:
        assert harness.listener.cfg.wake_word == "jarvis"
        harness.play(clip.audio, text=clip.text)
        harness.play(silence(1.5))

    assert "listening" in harness.face_states
    assert os.environ["JARVIS_CONFIG_PATH"] == str(user_config)
