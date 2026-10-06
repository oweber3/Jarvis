"""The ``scripts/enrol_voice.py`` command line, driven without a microphone."""

import numpy as np
import pytest

from jarvis.listening import voiceprint
from jarvis.listening.enrolment import enrol, main
from jarvis.listening.speaker_verification import SAMPLE_RATE
from tests.audio_harness import save_wav
from tests.test_voice_enrolment import _ToneEmbedder, _tone

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))


def _run(argv, *, clips=None):
    lines = []
    recordings = iter(clips or [])
    code = main(argv, recorder=lambda seconds: next(recordings), embedder=_ToneEmbedder(), out=lines.append)
    return code, "\n".join(lines)


def test_status_reports_whether_a_voice_is_enrolled():
    code, text = _run(["--status"])
    assert code == 0 and "not enrolled" in text.lower()
    enrol([_tone(220.0, seed=i) for i in range(4)], _ToneEmbedder())
    code, text = _run(["--status"])
    assert code == 0 and "enrolled" in text.lower() and "not enrolled" not in text.lower()


def test_delete_removes_the_voiceprint():
    enrol([_tone(220.0, seed=i) for i in range(4)], _ToneEmbedder())
    code, text = _run(["--delete"])
    assert code == 0 and not voiceprint.has_voiceprint()
    assert "deleted" in text.lower()


def test_deleting_when_nothing_is_enrolled_is_harmless():
    code, _ = _run(["--delete"])
    assert code == 0


def test_recording_enrols_after_prompting_for_each_phrase():
    clips = [_tone(220.0, seed=i) for i in range(6)]
    code, text = _run([], clips=clips)
    assert code == 0 and voiceprint.has_voiceprint()
    assert text.count("Phrase") >= 6


def test_a_failed_enrolment_exits_non_zero_and_stores_nothing():
    clips = [_tone(220.0, seconds=0.3, seed=i) for i in range(6)]
    code, _ = _run([], clips=clips)
    assert code != 0 and not voiceprint.has_voiceprint()


def test_wav_files_can_be_enrolled_without_a_microphone(tmp_path):
    paths = [str(save_wav(tmp_path / f"w{i}.wav", _tone(220.0, seed=i))) for i in range(4)]
    code, _ = _run(["--wav", *paths])
    assert code == 0 and voiceprint.has_voiceprint()


def test_output_is_emoji_led_and_never_shows_embedding_values():
    clips = [_tone(220.0, seed=i) for i in range(6)]
    _, text = _run([], clips=clips)
    for line in text.splitlines():
        if line.strip():
            assert not line.lstrip()[0].isalnum(), line
    assert "centroid" not in text.lower() and "[0." not in text


def test_a_missing_speaker_model_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SPEAKER_MODEL", str(tmp_path / "missing.onnx"))
    import jarvis.listening.speaker_verification as sv

    def no_network(*_a, **_k):
        raise RuntimeError("offline")

    monkeypatch.setattr(sv, "ensure_model", no_network)
    lines = []
    code = main([], recorder=lambda s: np.zeros(int(s * SAMPLE_RATE), dtype=np.float32), out=lines.append)
    assert code != 0
    assert any("speaker model" in line.lower() for line in lines)


@pytest.mark.interactive
def test_enrolling_from_the_real_microphone():
    """Run by hand: ``python scripts/enrol_voice.py`` and read the prompts aloud."""
    pytest.skip("needs a person and a microphone; run it by hand")
