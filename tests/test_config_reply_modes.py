"""Reply-mode settings: allowed cloud modes, Claude settings, migration and the minimal-config writer."""
import json

import pytest

from jarvis.config import get_default_config, load_settings, update_config_values

CLAUDE_SUFFIXES = ("timeout_sec", "queue_limit", "share_recent_dialogue", "recent_dialogue_messages",
                   "share_desktop_referents", "share_foreground_window", "share_long_term_memory",
                   "max_tool_calls")


def write(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return path


@pytest.mark.unit
class TestDefaults:
    def test_no_cloud_mode_is_allowed_by_default(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {})
        cfg = load_settings()
        assert cfg.reply_mode == "local"
        assert cfg.codex_enabled is False and cfg.claude_enabled is False
        defaults = get_default_config()
        assert defaults["codex_enabled"] is False and defaults["claude_enabled"] is False

    def test_claude_has_the_same_private_bounds_as_codex(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {})
        cfg = load_settings()
        defaults = get_default_config()
        for suffix in CLAUDE_SUFFIXES:
            assert defaults[f"claude_{suffix}"] == defaults[f"codex_{suffix}"], suffix
            assert getattr(cfg, f"claude_{suffix}") == defaults[f"claude_{suffix}"]
        assert cfg.claude_share_long_term_memory is False
        assert (cfg.claude_model, cfg.claude_effort, cfg.claude_executable) == (
            defaults["claude_model"], defaults["claude_effort"], defaults["claude_executable"])

    @pytest.mark.parametrize("mode", ["codex", "claude"])
    def test_the_foreground_window_is_shared_unless_turned_off(self, tmp_path, monkeypatch, mode):
        write(tmp_path, monkeypatch, {})
        assert getattr(load_settings(), f"{mode}_share_foreground_window") is True
        write(tmp_path, monkeypatch, {"_config_version": 6, f"{mode}_share_foreground_window": False})
        assert getattr(load_settings(), f"{mode}_share_foreground_window") is False


@pytest.mark.unit
class TestOverrides:
    def test_claude_is_a_reply_mode(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {"_config_version": 6, "reply_mode": " Claude", "claude_enabled": True,
                                      "claude_model": "haiku", "claude_effort": "", "claude_max_tool_calls": 3})
        cfg = load_settings()
        assert cfg.reply_mode == "claude" and cfg.claude_enabled is True
        assert (cfg.claude_model, cfg.claude_effort, cfg.claude_max_tool_calls) == ("haiku", "", 3)

    @pytest.mark.parametrize("key,value,expected", [
        ("claude_timeout_sec", -5, 5.0),
        ("claude_queue_limit", -3, 0),
        ("claude_max_tool_calls", 0, 1),
        ("claude_effort", "Medium ", "medium"),
    ])
    def test_invalid_claude_values_fall_back_safely(self, tmp_path, monkeypatch, key, value, expected):
        write(tmp_path, monkeypatch, {"_config_version": 6, key: value})
        assert getattr(load_settings(), key) == expected


@pytest.mark.unit
class TestMigration:
    def test_an_existing_codex_user_keeps_codex_allowed(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 5, "reply_mode": "codex", "other": {"x": 1}})
        cfg = load_settings()
        assert cfg.reply_mode == "codex" and cfg.codex_enabled is True
        on_disk = json.loads(path.read_text())
        assert on_disk["codex_enabled"] is True and on_disk["_config_version"] >= 6 and on_disk["other"] == {"x": 1}

    def test_local_users_are_not_opted_into_anything(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 5, "reply_mode": "local"})
        assert load_settings().codex_enabled is False
        assert "codex_enabled" not in json.loads(path.read_text())

    def test_an_explicit_choice_is_kept(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 5, "reply_mode": "codex", "codex_enabled": False})
        assert load_settings().codex_enabled is False
        assert json.loads(path.read_text())["codex_enabled"] is False


@pytest.mark.unit
class TestUpdateConfigValues:
    def test_writes_only_non_default_values_and_keeps_unknown_keys(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 6, "mcps": {"a": {}}, "reply_mode": "claude"})
        assert update_config_values({"reply_mode": "codex"})
        assert json.loads(path.read_text()) == {"_config_version": 6, "mcps": {"a": {}}, "reply_mode": "codex"}
        assert update_config_values({"reply_mode": get_default_config()["reply_mode"]})
        assert json.loads(path.read_text()) == {"_config_version": 6, "mcps": {"a": {}}}

    def test_creates_the_file_when_missing(self, tmp_path, monkeypatch):
        path = tmp_path / "nested" / "config.json"
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        assert update_config_values({"reply_mode": "claude"})
        assert json.loads(path.read_text()) == {"reply_mode": "claude"}
