"""Speaker verification and barge-in against real Piper voices and the real speaker model.

Voice X is "the owner", other Piper voices are other people, and Jarvis's own
TTS is another Piper voice leaking into the microphone. Skipped when the
speaker model or the Piper voices are not on this machine.
"""

import pytest

from jarvis.listening.speaker_verification import MIN_VERIFY_SECONDS, Verdict
from tests.audio_harness import ListenerHarness, silence
from tests.audio_harness import speakers

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not speakers.available(), reason=speakers.skip_reason()),
]

CONDITIONS = [None, "music10", "noise10"]


def _scores(voice, phrases, condition):
    centroid = speakers.enrolled_centroid()
    out = []
    for i, phrase in enumerate(phrases):
        audio = speakers.speak(phrase, voice)
        if len(audio) < MIN_VERIFY_SECONDS * 16000:
            continue
        out.append(float(speakers.embedder().embed(speakers.with_bed(audio, condition, seed=i)) @ centroid))
    return out


@pytest.mark.parametrize("condition", CONDITIONS)
def test_the_owner_is_recognised_on_phrases_they_never_enrolled(condition):
    verifier = speakers.make_verifier("strict")
    scores = _scores(speakers.OWNER, speakers.HELD_OUT_PHRASES, condition)
    assert scores
    accepted = sum(s >= verifier.accept_threshold for s in scores) / len(scores)
    assert accepted >= 0.95, scores


@pytest.mark.parametrize("condition", CONDITIONS)
def test_other_voices_are_rejected_in_soft_mode(condition):
    verifier = speakers.make_verifier("soft")
    scores = [s for v in speakers.other_voices() for s in _scores(v, speakers.HELD_OUT_PHRASES, condition)]
    rejected = sum(s < verifier.reject_threshold for s in scores) / len(scores)
    assert rejected >= 0.95, f"{rejected:.0%} rejected, highest score {max(scores):.2f}"


@pytest.mark.parametrize("condition", CONDITIONS)
def test_other_voices_are_rejected_in_strict_mode(condition):
    verifier = speakers.make_verifier("strict")
    scores = [s for v in speakers.other_voices() for s in _scores(v, speakers.HELD_OUT_PHRASES, condition)]
    rejected = sum(s < verifier.accept_threshold for s in scores) / len(scores)
    assert rejected >= 0.99, f"{rejected:.0%} rejected, highest score {max(scores):.2f}"


def test_the_verifier_gives_verdicts_on_real_audio():
    verifier = speakers.make_verifier("soft")
    assert verifier.check(speakers.speak(speakers.HELD_OUT_PHRASES[1], speakers.OWNER)) is Verdict.OWNER
    assert verifier.check(speakers.speak(speakers.HELD_OUT_PHRASES[1], speakers.other_voices()[0])) is Verdict.OTHER


# -- barge-in through the listener ---------------------------------------

def _barge_in_run(user_over_tts_db, *, owner_talks, tts_text=speakers.JARVIS_REPLY, user_offset=1.0,
                  user_voice=None, condition=None):
    tts_audio = speakers.speak(tts_text, speakers.JARVIS_TTS)
    user = None
    if owner_talks:
        user = speakers.speak(speakers.HELD_OUT_PHRASES[3], user_voice or speakers.OWNER)
    mic = speakers.room_with_jarvis_talking(tts_audio, user, user_offset=user_offset,
                                            user_over_tts_db=user_over_tts_db)
    mic = speakers.with_bed(mic, condition)
    with ListenerHarness(speaker_verifier=speakers.make_verifier("soft"), with_tts=True) as harness:
        harness.tts.speaking = True
        harness.listener.track_tts_start(tts_text)
        start = harness.now
        harness.play(mic, text="")
        harness.play(silence(2.0))
    ducked_at = harness.tts.time_of("duck")
    return harness, (None if ducked_at is None else ducked_at - (start + user_offset))


@pytest.mark.parametrize("user_over_tts_db", [12.0, 6.0])
def test_the_owner_talking_over_jarvis_ducks_it_quickly(user_over_tts_db):
    harness, delay = _barge_in_run(user_over_tts_db, owner_talks=True)
    assert delay is not None, "owner talking over Jarvis was never noticed"
    verify_s = harness.listener.cfg.barge_in_verify_ms / 1000.0
    assert 0 <= delay <= verify_s + 0.7, f"ducked {delay:.2f}s after the owner started"


def test_the_owner_over_music_still_ducks_jarvis():
    harness, delay = _barge_in_run(6.0, owner_talks=True, condition="music10")
    assert delay is not None and delay <= 1.0


def test_jarvis_hearing_only_itself_never_ducks():
    for text in (speakers.JARVIS_REPLY, "Sure. I have opened Word and set the volume to thirty percent."):
        harness, _ = _barge_in_run(0.0, owner_talks=False, tts_text=text)
        assert harness.tts.time_of("duck") is None, text


def test_another_person_talking_over_jarvis_does_not_duck_it():
    for voice in speakers.other_voices()[:4]:
        harness, delay = _barge_in_run(6.0, owner_talks=True, user_voice=voice)
        assert delay is None, f"{voice.label} ducked Jarvis"
