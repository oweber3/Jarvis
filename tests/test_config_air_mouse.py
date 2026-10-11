"""The air mouse settings: off by default, thresholds loaded, clamped and kept consistent."""
import json

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.gestures.air_mouse import AirMouseSettings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestAirMouseSettings:
    def test_off_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["air_mouse_enabled"] is False
        assert load(tmp_path, monkeypatch).air_mouse_enabled is False

    def test_only_a_real_true_turns_it_on(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"air_mouse_enabled": True}).air_mouse_enabled is True
        assert load(tmp_path, monkeypatch, {"air_mouse_enabled": "yes"}).air_mouse_enabled is False

    def test_defaults_match_the_gesture_model(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch)
        assert AirMouseSettings.from_config(cfg) == AirMouseSettings()

    def test_values_are_loaded(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"air_mouse_camera": 2, "air_mouse_hand": "left",
                                           "air_mouse_comfort_box": 0.3, "air_mouse_hold_ms": 600})
        loaded = AirMouseSettings.from_config(cfg)
        assert cfg.air_mouse_camera == 2
        assert (loaded.hand, loaded.comfort_box, loaded.hold) == ("left", 0.3, 0.6)

    def test_out_of_range_values_are_clamped(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"air_mouse_comfort_box": 5, "air_mouse_hold_ms": 1,
                                           "air_mouse_camera": -3, "air_mouse_smoothing": 0})
        assert cfg.air_mouse_comfort_box == 1.0
        assert cfg.air_mouse_hold_ms == 100
        assert cfg.air_mouse_camera == 0
        assert cfg.air_mouse_smoothing > 0

    def test_wrongly_typed_values_use_the_defaults(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"air_mouse_comfort_box": "wide", "air_mouse_hand": "middle",
                                           "air_mouse_pinch_start": None})
        defaults = get_default_config()
        assert cfg.air_mouse_comfort_box == defaults["air_mouse_comfort_box"]
        assert cfg.air_mouse_hand == defaults["air_mouse_hand"]
        assert cfg.air_mouse_pinch_start == defaults["air_mouse_pinch_start"]

    def test_the_release_threshold_is_always_above_the_start(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"air_mouse_pinch_start": 0.5, "air_mouse_pinch_release": 0.3})
        assert cfg.air_mouse_pinch_release > cfg.air_mouse_pinch_start

    def test_every_setting_is_in_the_settings_window(self):
        from desktop_app.settings_window import FIELD_METADATA
        shown = {meta.key for meta in FIELD_METADATA}
        assert {key for key in get_default_config() if key.startswith("air_mouse_")} <= shown
