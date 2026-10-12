"""Dictation never invents text from silence (dictation.spec.md, Silence and Whisper's invented phrases).

Whisper given silence or room noise often returns a stock phrase ("Thanks for watching!"). Dictation
must give an empty transcript instead, on the hotkey path and the web chat's microphone button alike.
Whisper is faked; the audio is synthetic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pynput")
pytest.importorskip("sounddevice")

RATE = 16000
INVENTED = "Thanks for watching!"


def _engine(model, cfg=None):
    from jarvis.dictation import dictation_engine as de

    return de.DictationEngine(
        whisper_model_ref=lambda: model,
        whisper_backend_ref=lambda: "faster-whisper",
        mlx_repo_ref=lambda: None,
        hotkey="ctrl+shift+d",
        cfg=cfg,
    )


def _whisper(*segments):
    model = MagicMock()
    model.transcribe.return_value = (
        [SimpleNamespace(text=text, no_speech_prob=p) for text, p in segments], None)
    return model


def _silence(seconds=2.0, level=0.0005):
    rng = np.random.default_rng(0)
    return (rng.standard_normal(int(RATE * seconds)) * level).astype("float32")


def _speech(seconds=2.0, level=0.2):
    t = np.arange(int(RATE * seconds)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * level).astype("float32")


def _cfg(**overrides):
    values = {"voice_min_energy": 0.02, "whisper_no_speech_threshold": 0.5}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_silence_gives_an_empty_transcript_without_asking_whisper():
    model = _whisper((INVENTED, 0.1))
    assert _engine(model, _cfg())._transcribe(_silence()) == ""
    model.transcribe.assert_not_called()


def test_a_segment_whisper_marks_as_not_speech_is_dropped():
    model = _whisper(("Open the report", 0.05), (INVENTED, 0.9))
    assert _engine(model, _cfg())._transcribe(_speech()) == "Open the report"


def test_speech_is_transcribed():
    model = _whisper(("Open the report", 0.05))
    assert _engine(model, _cfg())._transcribe(_speech()) == "Open the report"


def test_a_short_loud_word_in_silence_still_counts_as_speech():
    audio = np.concatenate([_silence(1.0), _speech(0.3), _silence(1.0)])
    model = _whisper(("yes", 0.1))
    assert _engine(model, _cfg())._transcribe(audio) == "yes"


def test_the_thresholds_follow_the_configuration():
    quiet_voice = _speech(level=0.01)
    model = _whisper(("hello", 0.6))
    assert _engine(model, _cfg())._transcribe(quiet_voice) == ""
    lenient = _cfg(voice_min_energy=0.001, whisper_no_speech_threshold=0.8)
    assert _engine(model, lenient)._transcribe(quiet_voice) == "hello"


def test_without_a_configuration_the_defaults_apply():
    from jarvis.config import get_default_config

    defaults = get_default_config()
    model = _whisper((INVENTED, defaults["whisper_no_speech_threshold"]))
    assert _engine(model)._transcribe(_speech()) == ""
    assert _engine(_whisper((INVENTED, 0.1)))._transcribe(_silence()) == ""
