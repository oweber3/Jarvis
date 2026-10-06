"""The windows_workspaces setting: default, loading and preservation of the user's file."""
import json

import pytest

from jarvis.config import get_default_config, load_settings

WORKSPACE = {"aliases": ["design project"], "items": [
    {"kind": "browser_window", "urls": ["https://example.test"], "monitor": "primary", "zone": "left"}]}


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestWorkspaceSetting:
    def test_nothing_is_configured_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["windows_workspaces"] == {}
        assert load(tmp_path, monkeypatch).windows_workspaces == {}

    def test_a_workspace_is_loaded_with_derived_labels(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"windows_workspaces": {"design": WORKSPACE}})
        assert list(cfg.windows_workspaces) == ["design"]
        assert cfg.windows_workspaces["design"]["items"][0]["label"] == "browser window 1"

    @pytest.mark.parametrize("bad", ["design", None, 3, ["design"], {"design": "x"}, {"design": {"items": []}}])
    def test_malformed_values_mean_no_workspaces(self, tmp_path, monkeypatch, bad):
        assert load(tmp_path, monkeypatch, {"windows_workspaces": bad}).windows_workspaces == {}

    def test_a_bad_workspace_does_not_hide_a_good_one(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"windows_workspaces": {"design": WORKSPACE, "bad": {"items": [{}]}}})
        assert list(cfg.windows_workspaces) == ["design"]

    def test_loading_never_rewrites_the_users_file(self, tmp_path, monkeypatch):
        raw = {"windows_workspaces": {"design": WORKSPACE, "bad": {"items": [{}]}}, "unknown_key": 1}
        load(tmp_path, monkeypatch, raw)
        assert json.loads((tmp_path / "config.json").read_text())["windows_workspaces"] == raw["windows_workspaces"]
        assert json.loads((tmp_path / "config.json").read_text())["unknown_key"] == 1
