"""Activity log settings: off by default, nothing shared with the cloud, safe handling of bad values."""
import json

import pytest

from jarvis.config import get_default_config, load_settings, update_config_values
from jarvis.memory import activity_log


def write(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return path


@pytest.mark.unit
class TestDefaults:
    def test_nothing_is_recorded_or_shared_by_default(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {})
        cfg = load_settings()
        assert cfg.activity_log_enabled is False
        assert cfg.activity_log_paused is False
        assert cfg.activity_log_share_with_cloud is False

    def test_exclusions_default_to_the_data_file(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {})
        cfg = load_settings()
        assert cfg.activity_log_excluded_processes == activity_log.default_excluded_processes()
        assert cfg.activity_log_private_title_markers == activity_log.default_private_title_markers()
        defaults = get_default_config()
        assert defaults["activity_log_excluded_processes"] == activity_log.default_excluded_processes()

    def test_retention_and_idle_defaults(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {})
        cfg = load_settings()
        defaults = get_default_config()
        assert cfg.activity_log_retention_days == defaults["activity_log_retention_days"] == 30
        assert cfg.activity_log_idle_after_sec == defaults["activity_log_idle_after_sec"]


@pytest.mark.unit
class TestOverrides:
    def test_only_a_real_true_opts_in(self, tmp_path, monkeypatch):
        for junk in ("true", 1, "yes", None):
            write(tmp_path, monkeypatch, {"activity_log_enabled": junk, "activity_log_share_with_cloud": junk})
            cfg = load_settings()
            assert cfg.activity_log_enabled is False and cfg.activity_log_share_with_cloud is False
        write(tmp_path, monkeypatch, {"activity_log_enabled": True, "activity_log_share_with_cloud": True})
        cfg = load_settings()
        assert cfg.activity_log_enabled is True and cfg.activity_log_share_with_cloud is True

    @pytest.mark.parametrize("key,value,expected", [
        ("activity_log_retention_days", 0, 1),
        ("activity_log_retention_days", "abc", 30),
        ("activity_log_retention_days", 90, 90),
        ("activity_log_idle_after_sec", 5, 30.0),
        ("activity_log_idle_after_sec", "abc", 300.0),
    ])
    def test_numbers_are_bounded(self, tmp_path, monkeypatch, key, value, expected):
        write(tmp_path, monkeypatch, {key: value})
        assert getattr(load_settings(), key) == expected

    def test_lists_are_replaced_by_the_users_own_and_junk_is_ignored(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {"activity_log_excluded_processes": ["Notepad", "", 5, "  "],
                                      "activity_log_private_title_markers": "not a list"})
        cfg = load_settings()
        assert cfg.activity_log_excluded_processes == ["Notepad"]
        assert cfg.activity_log_private_title_markers == activity_log.default_private_title_markers()

    def test_pausing_writes_only_the_non_default_value(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"unrelated": 1})
        assert update_config_values({"activity_log_paused": True})
        written = json.loads(path.read_text())
        assert written["unrelated"] == 1 and written["activity_log_paused"] is True
        assert update_config_values({"activity_log_paused": False})
        written = json.loads(path.read_text())
        assert written["unrelated"] == 1 and "activity_log_paused" not in written
