"""Enrolling the owner's voice from recorded phrases or WAV files."""

import numpy as np
import pytest

from jarvis.listening import voiceprint
from jarvis.listening.enrolment import EnrolmentError, enrol, enrol_from_wavs
from jarvis.listening.speaker_verification import SAMPLE_RATE, create_verifier
from tests.audio_harness import save_wav
from tests.audio_harness import speakers


def _tone(freq: float, seconds: float = 2.0, seed: int = 0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    rng = np.random.default_rng(seed)
    return (0.3 * np.sin(2 * np.pi * freq * t) + 0.01 * rng.standard_normal(t.size)).astype(np.float32)


class _ToneEmbedder:
    """Each dominant frequency is one 'speaker'; a little jitter keeps clips distinct."""

    DIRECTIONS = {220.0: [1, 0, 0, 0], 440.0: [0, 1, 0, 0], 880.0: [0, 0, 1, 0]}

    def embed(self, audio):
        spectrum = np.abs(np.fft.rfft(audio))
        freq = float(np.argmax(spectrum)) * SAMPLE_RATE / len(audio)
        base = np.array(self.DIRECTIONS[min(self.DIRECTIONS, key=lambda f: abs(f - freq))], dtype=np.float32)
        jitter = np.random.default_rng(int(np.abs(audio[:50]).sum() * 1e4) % 1000).normal(0, 0.03, 4)
        vec = base + jitter.astype(np.float32)
        return vec / np.linalg.norm(vec)


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))


@pytest.mark.unit
class TestEnrol:
    def test_enough_clips_of_one_voice_are_saved_as_a_voiceprint(self):
        clips = [_tone(220.0, seed=i) for i in range(5)]
        result = enrol(clips, _ToneEmbedder())
        assert result.accepted == 5 and result.rejected == []
        assert voiceprint.has_voiceprint()
        assert np.linalg.norm(voiceprint.load_voiceprint()) == pytest.approx(1.0, abs=1e-5)

    def test_too_few_usable_clips_saves_nothing(self):
        with pytest.raises(EnrolmentError):
            enrol([_tone(220.0, seed=i) for i in range(2)], _ToneEmbedder())
        assert not voiceprint.has_voiceprint()

    def test_clips_too_short_to_judge_are_skipped_and_reported(self):
        clips = [_tone(220.0, seed=i) for i in range(4)] + [_tone(220.0, seconds=0.4, seed=9)]
        result = enrol(clips, _ToneEmbedder())
        assert result.accepted == 4
        assert [index for index, _ in result.rejected] == [4]

    def test_a_clip_from_another_speaker_is_dropped_as_an_outlier(self):
        clips = [_tone(220.0, seed=i) for i in range(5)] + [_tone(880.0, seed=7)]
        result = enrol(clips, _ToneEmbedder())
        assert result.accepted == 5
        assert [index for index, _ in result.rejected] == [5]

    def test_everyone_disagreeing_enrols_nobody(self):
        clips = [_tone(220.0), _tone(440.0), _tone(880.0), _tone(220.0, seed=2), _tone(440.0, seed=2)]
        with pytest.raises(EnrolmentError):
            enrol(clips, _ToneEmbedder())
        assert not voiceprint.has_voiceprint()

    def test_audio_is_never_written_anywhere(self, tmp_path):
        enrol([_tone(220.0, seed=i) for i in range(4)], _ToneEmbedder())
        names = {p.name for p in tmp_path.iterdir()}
        assert names == {"config.json", voiceprint.VOICEPRINT_FILENAME}

    def test_wav_files_are_enrolled_at_16khz(self, tmp_path):
        paths = [str(save_wav(tmp_path / f"clip{i}.wav", _tone(220.0, seed=i))) for i in range(4)]
        result = enrol_from_wavs(paths, _ToneEmbedder())
        assert result.accepted == 4

    def test_re_enrolling_replaces_the_voiceprint(self):
        enrol([_tone(220.0, seed=i) for i in range(4)], _ToneEmbedder())
        first = voiceprint.load_voiceprint()
        enrol([_tone(440.0, seed=i) for i in range(4)], _ToneEmbedder())
        assert float(first @ voiceprint.load_voiceprint()) < 0.5


@pytest.mark.unit
@pytest.mark.skipif(not speakers.available(), reason=speakers.skip_reason())
def test_a_voice_enrolled_from_wav_files_is_recognised_by_the_real_model(tmp_path, monkeypatch):
    from jarvis.listening.speaker_verification import Verdict

    monkeypatch.setenv("JARVIS_SPEAKER_MODEL", str(speakers.model_path()))
    paths = [str(save_wav(tmp_path / f"enrol{i}.wav", speakers.speak(text, speakers.OWNER)))
             for i, text in enumerate(speakers.ENROL_PHRASES)]
    enrol_from_wavs(paths, speakers.embedder())

    class Cfg:
        speaker_verification = "soft"
        speaker_reject_threshold = 0.25
        speaker_accept_threshold = 0.45

    verifier = create_verifier(Cfg())
    assert verifier is not None
    held_out = speakers.HELD_OUT_PHRASES[1]
    assert verifier.check(speakers.speak(held_out, speakers.OWNER)) is Verdict.OWNER
    assert verifier.check(speakers.speak(held_out, speakers.other_voices()[0])) is Verdict.OTHER
