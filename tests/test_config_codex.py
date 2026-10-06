"""Background Codex settings: safe defaults, overrides, fail-safe validation and migration."""
import json

import pytest

from jarvis.config import get_default_config, load_settings

SHARED_SUFFIXES = ("timeout_sec", "queue_limit", "share_recent_dialogue", "recent_dialogue_messages",
                   "share_long_term_memory", "max_tool_calls")
DESKTOP_ONLY = ("codex_desktop_thread_id", "codex_desktop_chat_title", "codex_desktop_model_label",
                "codex_desktop_busy_labels", "codex_desktop_navigate_to_thread",
                "codex_desktop_accept_timeout_sec")


def write(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return cfg_path


def load(tmp_path, monkeypatch, values=None):
    write(tmp_path, monkeypatch, values or {})
    return load_settings()


@pytest.mark.unit
class TestDefaults:
    def test_local_mode_and_private_by_default(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch)
        defaults = get_default_config()
        assert cfg.reply_mode == defaults["reply_mode"] == "local"
        assert cfg.codex_share_long_term_memory is False
        assert (cfg.codex_model, cfg.codex_reasoning_effort, cfg.codex_executable) == (
            defaults["codex_model"], defaults["codex_reasoning_effort"], defaults["codex_executable"])

    def test_desktop_records_are_shared_unless_turned_off(self, tmp_path, monkeypatch):
        assert get_default_config()["codex_share_desktop_referents"] is True
        assert load(tmp_path, monkeypatch).codex_share_desktop_referents is True
        off = load(tmp_path, monkeypatch, {"codex_share_desktop_referents": False})
        assert off.codex_share_desktop_referents is False

    def test_every_codex_setting_has_a_default(self):
        defaults = get_default_config()
        for key in ("reply_mode", "codex_model", "codex_reasoning_effort", "codex_executable",
                    *(f"codex_{s}" for s in SHARED_SUFFIXES)):
            assert key in defaults, key
        assert not any(key.startswith("codex_desktop_") for key in defaults)


@pytest.mark.unit
class TestOverrides:
    def test_explicit_values_are_used(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {
            "reply_mode": "codex", "codex_model": "gpt-6-sol", "codex_reasoning_effort": "Medium ",
            "codex_executable": "C:/tools/codex.exe", "codex_timeout_sec": 45, "codex_queue_limit": 0,
            "codex_share_recent_dialogue": False, "codex_recent_dialogue_messages": 3,
            "codex_share_long_term_memory": True, "codex_max_tool_calls": 4,
        })
        assert cfg.reply_mode == "codex"
        assert (cfg.codex_model, cfg.codex_reasoning_effort, cfg.codex_executable) == (
            "gpt-6-sol", "medium", "C:/tools/codex.exe")
        assert cfg.codex_timeout_sec == 45.0 and cfg.codex_queue_limit == 0
        assert cfg.codex_share_recent_dialogue is False and cfg.codex_recent_dialogue_messages == 3
        assert cfg.codex_share_long_term_memory is True and cfg.codex_max_tool_calls == 4

    @pytest.mark.parametrize("value", ["CODEX ", "codex_cloud", "", None, 5, ["codex"]])
    def test_unrecognised_reply_mode_means_local(self, tmp_path, monkeypatch, value):
        expected = "codex" if value == "CODEX " else "local"
        assert load(tmp_path, monkeypatch, {"_config_version": 5, "reply_mode": value}).reply_mode == expected

    @pytest.mark.parametrize("key,value,expected", [
        ("codex_timeout_sec", -5, 5.0),
        ("codex_timeout_sec", "soon", 90.0),
        ("codex_queue_limit", -3, 0),
        ("codex_recent_dialogue_messages", -1, 0),
        ("codex_max_tool_calls", 0, 1),
    ])
    def test_invalid_numbers_fall_back_safely(self, tmp_path, monkeypatch, key, value, expected):
        assert getattr(load(tmp_path, monkeypatch, {key: value}), key) == expected

    @pytest.mark.parametrize("key", ["codex_model", "codex_reasoning_effort", "codex_executable"])
    def test_blank_text_falls_back_to_the_default(self, tmp_path, monkeypatch, key):
        assert getattr(load(tmp_path, monkeypatch, {key: "  "}), key) == get_default_config()[key]


@pytest.mark.unit
class TestMigration:
    def desktop_config(self):
        return {"_config_version": 4, "reply_mode": "codex_desktop", "codex_desktop_thread_id": "t-1",
                "codex_desktop_chat_title": "Jarvis", "codex_desktop_model_label": "GPT-6 Luna Light",
                "codex_desktop_busy_labels": ["Stop"], "codex_desktop_navigate_to_thread": True,
                "codex_desktop_accept_timeout_sec": 20, "codex_desktop_timeout_sec": 60,
                "codex_desktop_share_recent_dialogue": False, "codex_desktop_recent_dialogue_messages": 2,
                "codex_desktop_share_long_term_memory": True, "codex_desktop_max_tool_calls": 5,
                "codex_desktop_queue_limit": 0, "some_future_key": {"kept": True}, "tts_engine": "piper"}

    def test_an_explicit_desktop_opt_in_becomes_background_codex_with_its_bounds(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, self.desktop_config())
        cfg = load_settings()
        assert cfg.reply_mode == "codex"
        assert (cfg.codex_timeout_sec, cfg.codex_queue_limit, cfg.codex_max_tool_calls) == (60.0, 0, 5)
        assert cfg.codex_share_recent_dialogue is False and cfg.codex_recent_dialogue_messages == 2
        assert cfg.codex_share_long_term_memory is True
        on_disk = json.loads(path.read_text())
        assert on_disk["reply_mode"] == "codex" and on_disk["_config_version"] >= 5
        assert not any(key.startswith("codex_desktop_") for key in on_disk)
        for suffix in SHARED_SUFFIXES:
            assert on_disk[f"codex_{suffix}"] == self.desktop_config()[f"codex_desktop_{suffix}"]
        assert on_disk["some_future_key"] == {"kept": True} and on_disk["tts_engine"] == "piper"

    def test_visible_chat_settings_are_dropped(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, self.desktop_config())
        load_settings()
        on_disk = json.loads(path.read_text())
        assert not any(key in on_disk for key in DESKTOP_ONLY)

    def test_only_explicit_values_are_written(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 4, "reply_mode": "codex_desktop"})
        load_settings()
        on_disk = json.loads(path.read_text())
        assert on_disk["reply_mode"] == "codex"
        assert [key for key in on_disk if key.startswith("codex_")] == ["codex_enabled"]

    def test_local_mode_stays_local(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 4, "reply_mode": "local",
                                             "codex_desktop_timeout_sec": 30})
        assert load_settings().reply_mode == "local"
        on_disk = json.loads(path.read_text())
        assert on_disk["reply_mode"] == "local" and on_disk["codex_timeout_sec"] == 30

    def test_an_existing_new_key_wins_over_the_old_one(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, {"_config_version": 4, "codex_desktop_timeout_sec": 30,
                                             "codex_timeout_sec": 70})
        assert load_settings().codex_timeout_sec == 70.0
        assert "codex_desktop_timeout_sec" not in json.loads(path.read_text())

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        path = write(tmp_path, monkeypatch, self.desktop_config())
        load_settings()
        once = path.read_text()
        load_settings()
        assert path.read_text() == once
