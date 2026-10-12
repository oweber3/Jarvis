"""The gesture snapping settings: off by default, thresholds loaded, clamped and kept consistent."""
import json

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.gestures.window_snap import WindowSnapSettings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestWindowSnapSettings:
    def test_off_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["gesture_snap_enabled"] is False
        assert load(tmp_path, monkeypatch).gesture_snap_enabled is False

    def test_only_a_real_true_turns_it_on(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"gesture_snap_enabled": True}).gesture_snap_enabled is True
        assert load(tmp_path, monkeypatch, {"gesture_snap_enabled": "yes"}).gesture_snap_enabled is False

    def test_defaults_match_the_gesture_model(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch)
        assert WindowSnapSettings.from_config(cfg) == WindowSnapSettings()

    def test_values_are_loaded(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_camera": 2, "gesture_snap_hand": "left",
                                           "gesture_snap_comfort_box": 0.3, "gesture_snap_debounce_ms": 80})
        loaded = WindowSnapSettings.from_config(cfg)
        assert cfg.gesture_snap_camera == 2
        assert (loaded.hand, loaded.comfort_box, loaded.debounce) == ("left", 0.3, 0.08)

    def test_out_of_range_values_are_clamped(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_comfort_box": 5, "gesture_snap_debounce_ms": 9000,
                                           "gesture_snap_camera": -3, "gesture_snap_smoothing": 0})
        assert cfg.gesture_snap_comfort_box == 1.0
        assert cfg.gesture_snap_debounce_ms == 500
        assert cfg.gesture_snap_camera == 0
        assert cfg.gesture_snap_smoothing > 0

    def test_wrongly_typed_values_use_the_defaults(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_comfort_box": "wide", "gesture_snap_hand": "middle",
                                           "gesture_snap_pinch_start": None})
        defaults = get_default_config()
        assert cfg.gesture_snap_comfort_box == defaults["gesture_snap_comfort_box"]
        assert cfg.gesture_snap_hand == defaults["gesture_snap_hand"]
        assert cfg.gesture_snap_pinch_start == defaults["gesture_snap_pinch_start"]

    def test_the_release_threshold_is_always_above_the_start(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_pinch_start": 0.5, "gesture_snap_pinch_release": 0.3})
        assert cfg.gesture_snap_pinch_release > cfg.gesture_snap_pinch_start

    def test_every_setting_is_in_the_settings_window(self):
        from desktop_app.settings_window import FIELD_METADATA
        shown = {meta.key for meta in FIELD_METADATA}
        assert {key for key in get_default_config() if key.startswith("gesture_snap_")} <= shown

    def test_fist_and_scroll_settings_are_loaded_and_clamped(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_fist_start": 0.8, "gesture_snap_fist_release": 0.95,
                                           "gesture_snap_scroll_speed": 99})
        loaded = WindowSnapSettings.from_config(cfg)
        assert (loaded.fist_start, loaded.fist_release) == (0.8, 0.95)
        assert cfg.gesture_snap_scroll_speed == 30.0

    def test_the_fist_release_is_always_above_the_start(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_fist_start": 1.0, "gesture_snap_fist_release": 0.9})
        assert cfg.gesture_snap_fist_release > cfg.gesture_snap_fist_start
