"""Dictation never invents text from silence (dictation.spec.md, Silence and Whisper's invented phrases).

Whisper given silence or room noise often returns a stock phrase ("Thanks for watching!"). Dictation
must give an empty transcript instead, on the hotkey path and the web chat's microphone button alike.
Whisper is faked; the audio is synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pynput")
pytest.importorskip("sounddevice")

RATE = 16000
INVENTED = "Thanks for watching!"


def _engine(model, cfg=None, speech_check=None):
    from jarvis.dictation import dictation_engine as de

    return de.DictationEngine(
        whisper_model_ref=lambda: model,
        whisper_backend_ref=lambda: "faster-whisper",
        mlx_repo_ref=lambda: None,
        hotkey="ctrl+shift+d",
        cfg=cfg,
        speech_check=speech_check,
    )


def _hears_speech(_audio, _rate):
    return True


def _whisper(*segments):
    model = MagicMock()
    model.transcribe.return_value = (
        [SimpleNamespace(text=text, no_speech_prob=p) for text, p in segments], None)
    return model


def _silence(seconds=2.0):
    return np.zeros(int(RATE * seconds), dtype="float32")


def _hiss(seconds=2.0, level=0.002):
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(RATE * seconds)) * level).astype("float32")


def _tone(seconds=2.0, level=0.2):
    t = np.arange(int(RATE * seconds)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * level).astype("float32")


def _cfg(**overrides):
    values = {"voice_min_energy": 0.02, "whisper_no_speech_threshold": 0.5}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("audio", [_silence(), _hiss()], ids=["silence", "room hiss"])
def test_audio_without_speech_gives_an_empty_transcript_without_asking_whisper(audio):
    pytest.importorskip("faster_whisper")
    model = _whisper((INVENTED, 0.1))
    assert _engine(model, _cfg())._transcribe(audio) == ""
    model.transcribe.assert_not_called()


def test_a_segment_whisper_marks_as_not_speech_is_dropped():
    model = _whisper(("Open the report", 0.05), (INVENTED, 0.9))
    assert _engine(model, _cfg(), _hears_speech)._transcribe(_tone()) == "Open the report"


def test_speech_is_transcribed():
    model = _whisper(("Open the report", 0.05))
    assert _engine(model, _cfg(), _hears_speech)._transcribe(_tone()) == "Open the report"


def test_the_no_speech_threshold_follows_the_configuration():
    model = _whisper(("hello", 0.6))
    assert _engine(model, _cfg(), _hears_speech)._transcribe(_tone()) == ""
    assert _engine(model, _cfg(whisper_no_speech_threshold=0.8), _hears_speech)._transcribe(_tone()) == "hello"


def test_without_a_configuration_the_default_threshold_applies():
    from jarvis.config import get_default_config

    threshold = get_default_config()["whisper_no_speech_threshold"]
    assert _engine(_whisper((INVENTED, threshold)), None, _hears_speech)._transcribe(_tone()) == ""


def test_the_mlx_backend_drops_segments_marked_as_not_speech():
    import sys

    mlx = MagicMock()
    mlx.transcribe.return_value = {"text": "Open the report Thanks for watching!", "segments": [
        {"text": " Open the report", "no_speech_prob": 0.05},
        {"text": " " + INVENTED, "no_speech_prob": 0.95}]}
    from jarvis.dictation import dictation_engine as de

    with patch.dict(sys.modules, {"mlx_whisper": mlx}):
        engine = de.DictationEngine(
            whisper_model_ref=lambda: None, whisper_backend_ref=lambda: "mlx",
            mlx_repo_ref=lambda: "repo", hotkey="ctrl+shift+d", cfg=_cfg(), speech_check=_hears_speech)
        assert engine._transcribe(_tone()) == "Open the report"


class TestSpeechCheck:
    def test_silero_ignores_silence_and_hiss(self):
        pytest.importorskip("faster_whisper")
        from jarvis.dictation.dictation_engine import audio_has_speech

        assert audio_has_speech(_silence(), RATE, 0.02) is False
        assert audio_has_speech(_hiss(), RATE, 0.02) is False

    def test_without_silero_the_voice_level_decides(self):
        import builtins

        from jarvis.dictation.dictation_engine import audio_has_speech

        real_import = builtins.__import__

        def no_faster_whisper(name, *args, **kwargs):
            if name.startswith("faster_whisper"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", no_faster_whisper):
            assert audio_has_speech(_tone(level=0.2), RATE, 0.02) is True
            assert audio_has_speech(_hiss(level=0.002), RATE, 0.02) is False
            assert audio_has_speech(_tone(level=0.01), RATE, 0.005) is True

    def test_empty_audio_has_no_speech(self):
        from jarvis.dictation.dictation_engine import audio_has_speech

        assert audio_has_speech(np.zeros(0, dtype="float32"), RATE, 0.02) is False
