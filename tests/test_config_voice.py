"""The voice is plain config: an engine, a voice id and a speed per engine, so anything (Settings, a personality)
can name one. Piper stays the default everywhere."""
import json

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.output.kokoro import KOKORO_DEFAULT_VOICE, KOKORO_SPEED_RANGE, KOKORO_VOICES
from jarvis.output.piper_voices import PIPER_DEFAULT_VOICE, piper_voices


def _load(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return load_settings()


@pytest.mark.unit
def test_piper_is_the_default_engine_with_the_default_voices(tmp_path, monkeypatch):
    defaults = get_default_config()
    assert defaults["tts_engine"] == "piper"
    cfg = _load(tmp_path, monkeypatch, {})
    assert cfg.tts_engine == "piper"
    assert (cfg.tts_piper_voice, cfg.tts_kokoro_voice) == (PIPER_DEFAULT_VOICE, KOKORO_DEFAULT_VOICE)
    assert cfg.tts_kokoro_speed == defaults["tts_kokoro_speed"]


@pytest.mark.unit
def test_a_chosen_kokoro_voice_and_speed_are_kept(tmp_path, monkeypatch):
    voice = next(v for v in KOKORO_VOICES if v != KOKORO_DEFAULT_VOICE)
    piper = next(v for v in piper_voices() if v != PIPER_DEFAULT_VOICE)
    cfg = _load(tmp_path, monkeypatch, {"tts_engine": "Kokoro", "tts_kokoro_voice": voice,
                                        "tts_kokoro_speed": 1.25, "tts_piper_voice": piper})
    assert (cfg.tts_engine, cfg.tts_kokoro_voice, cfg.tts_kokoro_speed, cfg.tts_piper_voice) == \
        ("kokoro", voice, 1.25, piper)


@pytest.mark.unit
@pytest.mark.parametrize("values, expected", [
    ({"tts_engine": "elevenlabs"}, ("piper", KOKORO_DEFAULT_VOICE, PIPER_DEFAULT_VOICE)),
    ({"tts_kokoro_voice": "zz_nobody"}, ("piper", KOKORO_DEFAULT_VOICE, PIPER_DEFAULT_VOICE)),
    ({"tts_piper_voice": "xx_XX-nobody-low"}, ("piper", KOKORO_DEFAULT_VOICE, PIPER_DEFAULT_VOICE)),
])
def test_unknown_engines_and_voices_fall_back_to_the_defaults(tmp_path, monkeypatch, values, expected):
    cfg = _load(tmp_path, monkeypatch, values)
    assert (cfg.tts_engine, cfg.tts_kokoro_voice, cfg.tts_piper_voice) == expected


@pytest.mark.unit
@pytest.mark.parametrize("speed", [0.0, 9.0, "fast"])
def test_kokoro_speed_stays_in_its_range(tmp_path, monkeypatch, speed):
    cfg = _load(tmp_path, monkeypatch, {"tts_kokoro_speed": speed})
    low, high = KOKORO_SPEED_RANGE
    assert low <= cfg.tts_kokoro_speed <= high
