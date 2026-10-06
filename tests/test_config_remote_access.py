"""Phone access settings: off by default, bounded port, quick actions as a list of commands."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestRemoteAccessSettings:
    def test_phone_access_is_off_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["remote_access_enabled"] is False
        assert load(tmp_path, monkeypatch).remote_access_enabled is False

    def test_only_a_real_true_turns_it_on(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"remote_access_enabled": True}).remote_access_enabled is True
        assert load(tmp_path, monkeypatch, {"remote_access_enabled": "yes"}).remote_access_enabled is False

    def test_defaults_load_unchanged(self, tmp_path, monkeypatch):
        defaults = get_default_config()
        cfg = load(tmp_path, monkeypatch)
        for key in ("remote_access_host", "remote_access_port", "remote_access_allow_confirm",
                    "remote_access_quick_actions"):
            assert getattr(cfg, key) == defaults[key], key

    @pytest.mark.parametrize("bad", [0, 80, 70000, "8765x", None, True])
    def test_an_unusable_port_falls_back_to_the_default(self, tmp_path, monkeypatch, bad):
        default = get_default_config()["remote_access_port"]
        assert load(tmp_path, monkeypatch, {"remote_access_port": bad}).remote_access_port == default

    def test_a_valid_port_is_kept(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"remote_access_port": 9100}).remote_access_port == 9100

    def test_the_host_is_trimmed_and_blank_means_default(self, tmp_path, monkeypatch):
        default = get_default_config()["remote_access_host"]
        assert load(tmp_path, monkeypatch, {"remote_access_host": " 100.64.1.2 "}).remote_access_host == "100.64.1.2"
        assert load(tmp_path, monkeypatch, {"remote_access_host": "  "}).remote_access_host == default

    def test_quick_actions_keep_only_non_blank_text(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"remote_access_quick_actions": ["Pause the music", "", 5, " Next song "]})
        assert cfg.remote_access_quick_actions == ["Pause the music", "Next song"]

    def test_an_empty_quick_action_list_is_kept(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"remote_access_quick_actions": []}).remote_access_quick_actions == []

    def test_every_phone_setting_is_in_the_settings_window(self):
        from desktop_app.settings_window import CATEGORIES, FIELD_METADATA
        keys = {k for k in get_default_config() if k.startswith("remote_access_")}
        fields = {f.key: f for f in FIELD_METADATA}
        assert keys <= set(fields), sorted(keys - set(fields))
        assert {fields[k].category for k in keys} == {"remote"}
        assert "remote" in {key for key, _ in CATEGORIES}
