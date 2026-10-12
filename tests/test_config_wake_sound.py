"""The wake word sound settings: loaded from config, with a sensitivity kept inside its usable range."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestWakeSoundSettings:
    def test_an_empty_config_loads_the_defaults(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch)
        for key, value in get_default_config().items():
            if key.startswith("wake_sound_"):
                assert getattr(cfg, key) == value, key

    def test_it_can_be_switched_off(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"wake_sound_enabled": False}).wake_sound_enabled is False

    def test_a_custom_model_path_is_kept(self, tmp_path, monkeypatch):
        model = str(tmp_path / "my_wake_word.onnx")
        assert load(tmp_path, monkeypatch, {"wake_sound_model": model}).wake_sound_model == model

    def test_an_empty_model_falls_back_to_the_default(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"wake_sound_model": "  "})
        assert cfg.wake_sound_model == get_default_config()["wake_sound_model"]

    @pytest.mark.parametrize("value, expected", [(0.0, 0.05), (1.5, 0.95), ("loud", None), (0.3, 0.3)])
    def test_the_threshold_stays_usable(self, tmp_path, monkeypatch, value, expected):
        cfg = load(tmp_path, monkeypatch, {"wake_sound_threshold": value})
        assert cfg.wake_sound_threshold == (get_default_config()["wake_sound_threshold"] if expected is None else expected)
