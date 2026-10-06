"""The routines setting: default, loading as written, and the live set Jarvis starts with."""
import json

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.routines import store

MOVIE = {"aliases": ["film night"], "steps": [{"tool": "getTime"}]}


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestRoutinesSetting:
    def test_nothing_is_configured_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["routines"] == {}
        cfg = load(tmp_path, monkeypatch)
        assert cfg.routines == {} and store.current(cfg) == {}

    def test_routines_in_the_file_are_the_ones_jarvis_starts_with(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"routines": {"movie mode": MOVIE, "bad": {"steps": []}}})
        assert list(store.current(cfg)) == ["movie mode"]
        assert store.current(cfg)["movie mode"].aliases == ("film night",)

    @pytest.mark.parametrize("bad", ["movie", None, 3, ["movie mode"]])
    def test_a_value_that_is_not_an_object_means_no_routines(self, tmp_path, monkeypatch, bad):
        cfg = load(tmp_path, monkeypatch, {"routines": bad})
        assert cfg.routines == {} and store.current(cfg) == {}

    def test_loading_never_rewrites_the_users_routines(self, tmp_path, monkeypatch):
        raw = {"routines": {"movie mode": MOVIE, "bad": {"steps": []}}, "unknown_key": 1}
        load(tmp_path, monkeypatch, raw)
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["routines"] == raw["routines"] and saved["unknown_key"] == 1
