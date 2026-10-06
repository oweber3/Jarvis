"""Window placement settings: safe defaults, validation and preservation of user files."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


DISPLAY = r"\\.\DISPLAY2"


@pytest.mark.unit
class TestDefaults:
    def test_nothing_is_configured_by_default(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch)
        defaults = get_default_config()
        for key in ("windows_monitor_aliases", "windows_window_zones"):
            assert key in defaults
            assert getattr(cfg, key) == defaults[key] == {}


@pytest.mark.unit
class TestLoading:
    def test_aliases_and_zones_are_loaded(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {
            "windows_monitor_aliases": {"side": DISPLAY},
            "windows_window_zones": {DISPLAY: {"left": [0, 0, 0.5, 1], "right": [0.5, 0, 0.5, 1]}},
        })
        assert cfg.windows_monitor_aliases == {"side": DISPLAY}
        assert cfg.windows_window_zones == {DISPLAY: {"left": [0.0, 0.0, 0.5, 1.0],
                                                      "right": [0.5, 0.0, 0.5, 1.0]}}

    def test_invalid_alias_entries_are_ignored(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"windows_monitor_aliases": {
            "side": DISPLAY, "": DISPLAY, "blank": "  ", "number": 3, "none": None}})
        assert cfg.windows_monitor_aliases == {"side": DISPLAY}

    @pytest.mark.parametrize("bad", ["left", None, 3, ["side"]])
    def test_non_mapping_aliases_mean_none(self, tmp_path, monkeypatch, bad):
        assert load(tmp_path, monkeypatch, {"windows_monitor_aliases": bad}).windows_monitor_aliases == {}

    @pytest.mark.parametrize("zone", [
        [0, 0, 1], [0, 0, 1, 1, 1], "left", None, 5, {"x": 0}, [0, 0, 0, 1], [0, 0, 1, 0],
        [0, 0, -1, 1], [-0.1, 0, 0.5, 1], [0.6, 0, 0.5, 1], [0, 0, 1.5, 1], [float("nan"), 0, 1, 1],
        [True, 0, 1, 1], ["0", 0, 1, 1],
    ])
    def test_invalid_zones_are_unavailable_but_valid_siblings_survive(self, tmp_path, monkeypatch, zone):
        cfg = load(tmp_path, monkeypatch, {"windows_window_zones": {
            DISPLAY: {"good": [0, 0, 0.5, 1], "bad": zone}}})
        assert list(cfg.windows_window_zones[DISPLAY]) == ["good"]

    def test_malformed_zone_tables_are_ignored(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"windows_window_zones": {
            DISPLAY: "left", "": {"a": [0, 0, 1, 1]}, "empty": {"": [0, 0, 1, 1]},
            r"\\.\DISPLAY1": {"full": [0, 0, 1, 1]}}})
        assert cfg.windows_window_zones == {r"\\.\DISPLAY1": {"full": [0.0, 0.0, 1.0, 1.0]}}
        assert load(tmp_path, monkeypatch, {"windows_window_zones": []}).windows_window_zones == {}

    def test_invalid_entries_are_logged_without_labels_or_identifiers(self, tmp_path, monkeypatch):
        messages = []
        monkeypatch.setattr("jarvis.debug.debug_log", lambda message, *args, **kwargs: messages.append(message))
        load(tmp_path, monkeypatch, {"windows_window_zones": {"SECRET-DEVICE": {"private-label": [9, 9, 9, 9]}},
                                     "windows_monitor_aliases": {"private-alias": 3}})
        assert messages
        assert not any(word in message for message in messages
                       for word in ("SECRET-DEVICE", "private-label", "private-alias"))


@pytest.mark.unit
class TestUserFilesAreNotRewritten:
    def test_loading_keeps_unknown_keys_and_invalid_entries_on_disk(self, tmp_path, monkeypatch):
        original = {"future_option": {"keep": True}, "windows_window_zones": {DISPLAY: {"bad": [9, 9, 9, 9]}},
                    "windows_monitor_aliases": {"side": DISPLAY}}
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(original))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        load_settings()
        stored = json.loads(cfg_path.read_text())
        assert {key: stored[key] for key in original} == original


@pytest.mark.unit
class TestFancyZonesSetting:
    def test_fancyzones_are_used_unless_switched_off(self, tmp_path, monkeypatch):
        assert get_default_config()["windows_fancyzones_enabled"] is True
        assert load(tmp_path, monkeypatch).windows_fancyzones_enabled is True
        assert load(tmp_path, monkeypatch, {"windows_fancyzones_enabled": False}).windows_fancyzones_enabled is False

    def test_the_toggle_is_offered_on_the_windows_settings_page(self):
        from desktop_app.settings_window import FIELD_METADATA
        field = next(item for item in FIELD_METADATA if item.key == "windows_fancyzones_enabled")
        assert field.category == "windows" and field.field_type == "bool"
