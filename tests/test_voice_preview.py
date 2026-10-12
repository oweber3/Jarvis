"""The voice preview speaks a sample line with exactly the engine, voice and speed being chosen."""
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis.output import kokoro, voice_preview
from jarvis.output.piper_voices import piper_voice_path
from jarvis.output.tts import PiperTTS


class RecordingPiper:
    """Stands in for Piper's loader and records which voice file each engine asked for."""

    def __init__(self):
        self.paths = []

    def __call__(self, engine):
        self.paths.append(engine.model_path)

        class Voice:
            config = SimpleNamespace(sample_rate=22050)

            def synthesize(self, text, config):
                assert config.length_scale == engine.length_scale
                yield SimpleNamespace(audio_int16_array=np.full(4410, 900, dtype=np.int16))

        engine._voice = Voice()
        engine._sample_rate = 22050


@pytest.fixture
def piper(monkeypatch):
    recorder = RecordingPiper()
    monkeypatch.setattr(PiperTTS, "_load_voice", lambda engine: recorder(engine))
    monkeypatch.setattr(voice_preview, "piper_voice_downloaded", lambda key: True)
    return recorder


class FakeKokoro:
    sample_rate = kokoro.SAMPLE_RATE
    device = "processor"

    def __init__(self, voice, speed):
        self.voice, self.speed = voice, speed

    def synthesize(self, text, _config):
        yield SimpleNamespace(audio_int16_array=np.full(int(2400 / self.speed), 1200, dtype=np.int16))


@pytest.mark.unit
def test_piper_preview_uses_the_chosen_voice_and_speed(piper):
    result = voice_preview.render_preview({"tts_engine": "piper", "tts_piper_voice": "en_US-ryan-high",
                                           "tts_piper_length_scale": 0.9}, "Hello.")
    assert piper.paths == [str(piper_voice_path("en_US-ryan-high"))]
    assert result.engine == "piper" and result.note is None
    assert result.sample_rate == 22050 and np.abs(result.samples).max() > 0


@pytest.mark.unit
def test_a_custom_piper_file_wins_over_the_catalogued_voice(piper, tmp_path):
    custom = str(tmp_path / "mine.onnx")
    voice_preview.render_preview({"tts_engine": "piper", "tts_piper_voice": "en_US-ryan-high",
                                  "tts_piper_model_path": custom}, "Hello.")
    assert piper.paths == [custom]


@pytest.mark.unit
def test_kokoro_preview_downloads_its_files_first_and_speaks_with_kokoro(piper, monkeypatch):
    present = {"now": False}
    progress = []

    def download(models_dir=None, progress=None):
        progress("📥 Kokoro kokoro-v1.0.onnx: 50%|| ...")
        present["now"] = True
        return True

    def load(models_dir=None, voice=kokoro.KOKORO_DEFAULT_VOICE, speed=1.0):
        if not present["now"]:
            raise kokoro.KokoroUnavailable("the Kokoro voice files are not downloaded yet")
        return FakeKokoro(voice, speed)

    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: present["now"])
    monkeypatch.setattr(kokoro, "download_kokoro", download)
    monkeypatch.setattr(kokoro, "load_kokoro_voice", load)

    result = voice_preview.render_preview({"tts_engine": "kokoro", "tts_kokoro_voice": "bf_emma",
                                           "tts_kokoro_speed": 1.5}, "Hello.", progress.append)
    assert result.engine == "kokoro" and result.note is None
    assert result.sample_rate == kokoro.SAMPLE_RATE and len(result.samples) == 1600
    assert progress and piper.paths == []


@pytest.mark.unit
def test_kokoro_that_cannot_download_previews_piper_and_says_why(piper, monkeypatch):
    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: False)
    monkeypatch.setattr(kokoro, "download_kokoro", lambda *a, **k: False)
    result = voice_preview.render_preview({"tts_engine": "kokoro"}, "Hello.")
    assert result.engine == "piper"
    assert "download" in result.note.lower()
    assert np.abs(result.samples).max() > 0


@pytest.mark.unit
def test_chatterbox_has_no_preview():
    with pytest.raises(voice_preview.PreviewUnavailable):
        voice_preview.render_preview({"tts_engine": "chatterbox"}, "Hello.")


@pytest.mark.unit
def test_a_voice_that_cannot_load_explains_itself(monkeypatch):
    def broken(engine):
        engine._init_error = "Failed to download voice: en_GB-alan-medium"

    monkeypatch.setattr(PiperTTS, "_load_voice", broken)
    monkeypatch.setattr(voice_preview, "piper_voice_downloaded", lambda key: True)
    with pytest.raises(voice_preview.PreviewUnavailable, match="download"):
        voice_preview.render_preview({"tts_engine": "piper"}, "Hello.")
