"""The gesture snapping settings: off by default, finger thresholds loaded, clamped and kept consistent."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestGestureSnapSettings:
    def test_off_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["gesture_snap_enabled"] is False
        assert load(tmp_path, monkeypatch).gesture_snap_enabled is False

    def test_only_a_real_true_turns_it_on(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"gesture_snap_enabled": True}).gesture_snap_enabled is True
        assert load(tmp_path, monkeypatch, {"gesture_snap_enabled": "yes"}).gesture_snap_enabled is False

    def test_an_empty_config_loads_the_defaults(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch)
        for key, value in get_default_config().items():
            if key.startswith("gesture_snap_"):
                assert getattr(cfg, key) == value, key

    def test_the_default_finger_down_threshold_is_below_up(self):
        defaults = get_default_config()
        assert defaults["gesture_snap_finger_down"] < defaults["gesture_snap_finger_up"]

    def test_values_are_loaded(self, tmp_path, monkeypatch):
        values = {"gesture_snap_camera": 2, "gesture_snap_hand": "left", "gesture_snap_comfort_box": 0.3,
                  "gesture_snap_debounce_ms": 80, "gesture_snap_finger_up": 1.2,
                  "gesture_snap_finger_down": 0.9, "gesture_snap_scroll_speed": 7.5}
        cfg = load(tmp_path, monkeypatch, values)
        for key, value in values.items():
            assert getattr(cfg, key) == value, key

    def test_out_of_range_values_are_clamped(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_comfort_box": 5, "gesture_snap_debounce_ms": 9000,
                                           "gesture_snap_camera": -3, "gesture_snap_smoothing": 0,
                                           "gesture_snap_scroll_speed": 99, "gesture_snap_finger_up": 9,
                                           "gesture_snap_finger_down": 0.1})
        assert cfg.gesture_snap_comfort_box == 1.0
        assert cfg.gesture_snap_debounce_ms == 500
        assert cfg.gesture_snap_camera == 0
        assert cfg.gesture_snap_smoothing > 0
        assert cfg.gesture_snap_scroll_speed == 30.0
        assert cfg.gesture_snap_finger_up == 2.0
        assert cfg.gesture_snap_finger_down == 0.5

    def test_wrongly_typed_values_use_the_defaults(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_comfort_box": "wide", "gesture_snap_hand": "middle",
                                           "gesture_snap_finger_up": None, "gesture_snap_finger_down": True,
                                           "gesture_snap_debounce_ms": "fast"})
        defaults = get_default_config()
        for key in ("gesture_snap_comfort_box", "gesture_snap_hand", "gesture_snap_finger_up",
                    "gesture_snap_finger_down", "gesture_snap_debounce_ms"):
            assert getattr(cfg, key) == defaults[key], key

    @pytest.mark.parametrize("up, down", [(1.0, 1.2), (1.1, 1.1), (0.8, 1.5), (2.0, 1.5)])
    def test_the_finger_down_threshold_is_always_below_up(self, tmp_path, monkeypatch, up, down):
        cfg = load(tmp_path, monkeypatch, {"gesture_snap_finger_up": up, "gesture_snap_finger_down": down})
        assert cfg.gesture_snap_finger_down < cfg.gesture_snap_finger_up
        assert cfg.gesture_snap_finger_down >= 0.5

    def test_every_setting_is_in_the_settings_window(self):
        from desktop_app.settings_window import FIELD_METADATA
        shown = {meta.key for meta in FIELD_METADATA}
        assert {key for key in get_default_config() if key.startswith("gesture_snap_")} <= shown

    def test_every_gesture_snap_setting_in_the_settings_window_is_a_config_key(self):
        from desktop_app.settings_window import FIELD_METADATA
        defaults = get_default_config()
        shown = {meta.key for meta in FIELD_METADATA if meta.key.startswith("gesture_snap_")}
        assert shown <= set(defaults)
