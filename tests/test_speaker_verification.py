"""Speaker verification: features, verdict modes and the local voiceprint store.

The model-backed accuracy tests live in ``test_speaker_verification_corpus.py``;
these cover the mechanisms with a stand-in embedder so they always run.
"""

import numpy as np
import pytest

from jarvis.listening.speaker_verification import (
    MIN_VERIFY_SECONDS,
    SAMPLE_RATE,
    SpeakerVerifier,
    Verdict,
    compute_fbank,
    create_verifier,
)
from jarvis.listening import voiceprint


def _tone(seconds: float, freq: float = 220.0, seed: int = 0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    rng = np.random.default_rng(seed)
    return (0.3 * np.sin(2 * np.pi * freq * t) + 0.02 * rng.standard_normal(t.size)).astype(np.float32)


class _FixedEmbedder:
    """Embeds any audio as a direction chosen by its dominant tone, so scores are exact."""

    def __init__(self, scores_by_freq: dict):
        self.scores_by_freq = scores_by_freq
        self.calls = 0

    def embed(self, audio):
        self.calls += 1
        spectrum = np.abs(np.fft.rfft(audio))
        freq = float(np.argmax(spectrum)) * SAMPLE_RATE / len(audio)
        nearest = min(self.scores_by_freq, key=lambda f: abs(f - freq))
        score = self.scores_by_freq[nearest]
        vec = np.zeros(4, dtype=np.float32)
        vec[0] = score
        vec[1] = np.sqrt(max(0.0, 1.0 - score * score))
        return vec


OWNER = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)


@pytest.mark.unit
class TestVerdicts:
    def _verifier(self, mode):
        embedder = _FixedEmbedder({220.0: 0.8, 440.0: 0.35, 880.0: 0.05})
        return SpeakerVerifier(embedder, OWNER, mode=mode, reject_threshold=0.25, accept_threshold=0.5)

    def test_owner_is_accepted_in_both_modes(self):
        for mode in ("soft", "strict"):
            assert self._verifier(mode).check(_tone(1.0, 220.0)) is Verdict.OWNER

    def test_confidently_other_speaker_is_rejected_in_both_modes(self):
        for mode in ("soft", "strict"):
            assert self._verifier(mode).check(_tone(1.0, 880.0)) is Verdict.OTHER

    def test_ambiguous_speaker_passes_soft_and_fails_strict(self):
        assert self._verifier("soft").allows(_tone(1.0, 440.0))
        assert not self._verifier("strict").allows(_tone(1.0, 440.0))

    def test_audio_too_short_to_verify_is_unknown_and_allowed(self):
        short = _tone(MIN_VERIFY_SECONDS / 2, 880.0)
        for mode in ("soft", "strict"):
            verifier = self._verifier(mode)
            assert verifier.check(short) is Verdict.UNKNOWN
            assert verifier.allows(short)
            assert verifier.embedder.calls == 0

    def test_score_is_cosine_similarity_to_the_voiceprint(self):
        assert self._verifier("soft").score(_tone(1.0, 220.0)) == pytest.approx(0.8, abs=1e-4)

    def test_an_embedder_failure_never_blocks_the_owner(self):
        class Broken:
            def embed(self, audio):
                raise RuntimeError("model fell over")

        verifier = SpeakerVerifier(Broken(), OWNER, mode="strict")
        assert verifier.check(_tone(1.0)) is Verdict.UNKNOWN
        assert verifier.allows(_tone(1.0))

    @pytest.mark.parametrize("owner", [
        np.ones(3, dtype=np.float32),             # enrolled with a model of another size
        np.full(4, np.nan, dtype=np.float32),     # a malformed voiceprint
    ], ids=["other model size", "not finite"])
    def test_a_voiceprint_that_does_not_fit_the_model_never_blocks_speech(self, owner):
        embedder = _FixedEmbedder({220.0: 0.8})
        for mode in ("soft", "strict"):
            verifier = SpeakerVerifier(embedder, owner, mode=mode)
            assert verifier.check(_tone(1.0)) is Verdict.UNKNOWN
            assert verifier.allows(_tone(1.0))


@pytest.mark.unit
class TestFbank:
    def test_shape_follows_25ms_windows_every_10ms_with_80_mel_bins(self):
        feats = compute_fbank(_tone(1.0))
        assert feats.shape == (98, 80)
        assert feats.dtype == np.float32

    def test_features_are_mean_normalised_over_time(self):
        feats = compute_fbank(_tone(1.0))
        assert np.abs(feats.mean(axis=0)).max() < 1e-3

    def test_matches_the_kaldi_reference_implementation(self):
        torch = pytest.importorskip("torch")
        torchaudio = pytest.importorskip("torchaudio")
        rng = np.random.default_rng(3)
        audio = (0.2 * rng.standard_normal(SAMPLE_RATE * 2)).astype(np.float32)
        wave = torch.from_numpy(audio).unsqueeze(0) * 32768
        ref = torchaudio.compliance.kaldi.fbank(
            wave, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
            sample_frequency=SAMPLE_RATE, window_type="hamming", use_energy=False)
        ref = (ref - ref.mean(0, keepdim=True)).numpy()
        ours = compute_fbank(audio)
        assert ours.shape == ref.shape
        assert np.abs(ours - ref).max() < 0.05


@pytest.mark.unit
class TestVoiceprintStore:
    @pytest.fixture(autouse=True)
    def _config_dir(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
        self.dir = tmp_path

    def test_nothing_is_enrolled_by_default(self):
        assert not voiceprint.has_voiceprint()
        assert voiceprint.load_voiceprint() is None

    def test_enrolment_round_trips_a_unit_length_centroid(self):
        embeddings = np.array([[2.0, 0.0, 0.0], [2.0, 2.0, 0.0]], dtype=np.float32)
        voiceprint.save_voiceprint(embeddings)
        centroid = voiceprint.load_voiceprint()
        assert voiceprint.has_voiceprint()
        assert np.linalg.norm(centroid) == pytest.approx(1.0, abs=1e-5)
        unit_rows = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)
        mean_direction = unit_rows.mean(0) / np.linalg.norm(unit_rows.mean(0))
        assert float(centroid @ mean_direction) == pytest.approx(1.0, abs=1e-4)

    def test_voiceprint_lives_beside_the_config_and_nowhere_else(self):
        voiceprint.save_voiceprint(np.ones((1, 3), dtype=np.float32))
        assert voiceprint.voiceprint_path().parent == self.dir
        assert voiceprint.voiceprint_path().exists()

    def test_delete_removes_the_voiceprint(self):
        voiceprint.save_voiceprint(np.ones((1, 3), dtype=np.float32))
        assert voiceprint.delete_voiceprint() is True
        assert not voiceprint.has_voiceprint()
        assert voiceprint.delete_voiceprint() is False

    def test_a_corrupt_file_reads_as_not_enrolled(self):
        voiceprint.voiceprint_path().write_bytes(b"not an npz")
        assert voiceprint.load_voiceprint() is None

    def test_saving_and_loading_never_print_or_log_the_embedding(self, capsys, monkeypatch):
        logged = []
        monkeypatch.setattr(voiceprint, "debug_log", lambda msg, *a, **k: logged.append(msg))
        embeddings = np.full((1, 3), 0.123456, dtype=np.float32)
        voiceprint.save_voiceprint(embeddings)
        voiceprint.load_voiceprint()
        out = capsys.readouterr()
        assert "0.1234" not in out.out + out.err + " ".join(logged)


@pytest.mark.unit
class TestCreateVerifier:
    class _Cfg:
        speaker_verification = "off"
        speaker_reject_threshold = 0.25
        speaker_accept_threshold = 0.5

    @pytest.fixture(autouse=True)
    def _config_dir(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
        monkeypatch.setenv("JARVIS_SPEAKER_MODEL", str(tmp_path / "missing.onnx"))

    def test_off_creates_nothing(self):
        assert create_verifier(self._Cfg()) is None

    def test_enabled_without_an_enrolled_voiceprint_falls_back_to_off(self):
        cfg = self._Cfg()
        cfg.speaker_verification = "soft"
        assert create_verifier(cfg) is None

    def test_enabled_without_the_model_file_falls_back_to_off(self):
        cfg = self._Cfg()
        cfg.speaker_verification = "strict"
        voiceprint.save_voiceprint(np.ones((1, 256), dtype=np.float32))
        assert create_verifier(cfg) is None


@pytest.mark.unit
class TestLastScore:
    def _verifier(self):
        embedder = _FixedEmbedder({220.0: 0.8, 880.0: 0.05})
        return SpeakerVerifier(embedder, OWNER, mode="soft", reject_threshold=0.25, accept_threshold=0.5)

    def test_the_last_score_is_kept_for_debugging(self):
        verifier = self._verifier()
        assert verifier.last_score is None
        verifier.check(_tone(1.0, 220.0))
        assert verifier.last_score == pytest.approx(0.8, abs=1e-4)

    def test_a_short_clip_clears_the_last_score(self):
        verifier = self._verifier()
        verifier.check(_tone(1.0, 220.0))
        verifier.check(_tone(0.2, 220.0))
        assert verifier.last_score is None


@pytest.mark.unit
class TestDeletedVoiceprint:
    """Deleting the voiceprint (Settings or the script) stops verification at once, without a restart."""

    def test_after_deletion_nothing_is_judged_and_the_template_is_dropped(self, tmp_path):
        stored = tmp_path / "voiceprint.npz"
        stored.write_bytes(b"x")
        now = [100.0]
        embedder = _FixedEmbedder({220.0: 0.8, 880.0: 0.05})
        verifier = SpeakerVerifier(embedder, OWNER, mode="strict", voiceprint_file=stored, clock=lambda: now[0])
        assert verifier.check(_tone(1.0, 880.0)) is Verdict.OTHER
        stored.unlink()
        now[0] += 2.0
        assert verifier.check(_tone(1.0, 880.0)) is Verdict.UNKNOWN
        assert verifier.score(_tone(1.0, 220.0), min_seconds=0.25) is None
        assert not verifier.enrolled

    def test_the_file_is_checked_at_most_once_a_second(self, tmp_path, monkeypatch):
        from pathlib import Path
        stored = tmp_path / "voiceprint.npz"
        stored.write_bytes(b"x")
        checks = []
        real = Path.is_file
        monkeypatch.setattr(Path, "is_file", lambda self: checks.append(1) or real(self))
        verifier = SpeakerVerifier(_FixedEmbedder({220.0: 0.8}), OWNER, voiceprint_file=stored, clock=lambda: 5.0)
        for _ in range(20):
            verifier.score(_tone(0.5), min_seconds=0.25)
        assert len(checks) <= 1

    def test_a_verifier_without_a_file_keeps_working(self):
        verifier = SpeakerVerifier(_FixedEmbedder({220.0: 0.8}), OWNER)
        assert verifier.check(_tone(1.0)) is Verdict.OWNER
