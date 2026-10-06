"""After a wake, speech that is confidently not the owner is ignored."""

import pytest

from jarvis.listening.speaker_verification import Verdict
from tests.audio_harness import ListenerHarness, fixture, silence
from tests.audio_harness.stubs import StubVerifier

pytestmark = pytest.mark.unit


def _say(harness, clip, tail=3.0):
    harness.play(clip.audio, text=clip.text)
    harness.play(silence(tail))


def test_the_owner_waking_jarvis_is_dispatched():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(Verdict.OWNER)) as harness:
        _say(harness, clip)
    assert harness.dispatched and "time" in harness.dispatched[0].lower()


def test_another_voice_saying_the_wake_word_is_ignored():
    clip = fixture("wake_command__jarvis_what_time_is_it__ryan.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(Verdict.OTHER)) as harness:
        _say(harness, clip)
    assert harness.whisper_calls  # it was still transcribed, as ambient context
    assert harness.dispatched == []
    assert harness.collections == []
    assert "listening" not in harness.face_states


def test_a_voice_that_cannot_be_judged_is_not_blocked():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness(speaker_verifier=StubVerifier(Verdict.UNKNOWN)) as harness:
        _say(harness, clip)
    assert harness.dispatched


def test_a_stranger_cannot_complete_the_owners_bare_wake():
    wake = fixture("wake__jarvis__alan.wav")
    request = fixture("wake_command__jarvis_what_time_is_it__ryan.wav")
    verifier = StubVerifier(Verdict.OWNER, Verdict.OTHER)
    with ListenerHarness(speaker_verifier=verifier) as harness:
        harness.play(wake.audio, text=wake.text)
        harness.play(silence(1.0))
        harness.play(request.audio, text="what time is it")
        harness.play(silence(1.0))
        assert harness.dispatched == []


def test_every_utterance_is_verified_once_on_the_whisper_side():
    clips = [fixture("speech__can_you_pass_me_the_salt_and_the_pepper_please__lessac.wav"),
             fixture("wake__jarvis__alan.wav")]
    verifier = StubVerifier()
    with ListenerHarness(speaker_verifier=verifier) as harness:
        for clip in clips:
            _say(harness, clip, tail=1.5)
    assert verifier.calls == len(harness.whisper_calls) == 2


def test_without_a_verifier_nothing_changes():
    clip = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness() as harness:
        assert harness.listener._speaker_verifier is None
        _say(harness, clip)
    assert harness.dispatched


def test_speech_captured_during_tts_is_not_gated_on_voice():
    # Jarvis's own voice is "another speaker"; echo and stop handling own that case.
    clip = fixture("wake__jarvis__alan.wav")
    verifier = StubVerifier(default=Verdict.OTHER)
    with ListenerHarness(speaker_verifier=verifier, with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start("The time is ten past four.")
        harness.play(clip.audio, text="stop")
        harness.play(silence(1.5))
    assert verifier.calls == 0


def test_a_verifier_that_fails_never_stops_jarvis_hearing_the_owner():
    class Exploding(StubVerifier):
        def check(self, audio, **_kwargs):
            self.calls += 1
            raise ValueError("voiceprint does not fit the model")

    first = fixture("wake_command__jarvis_what_time_is_it__alba.wav")
    with ListenerHarness(speaker_verifier=Exploding()) as harness:
        _say(harness, first)
        _say(harness, first)
    assert len(harness.dispatched) >= 1 and "time" in harness.dispatched[0].lower()
