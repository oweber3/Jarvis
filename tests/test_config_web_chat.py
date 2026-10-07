"""Web chat settings: off by default, bounded loopback port."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestWebChatSettings:
    def test_the_web_chat_is_off_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["web_chat_enabled"] is False
        assert load(tmp_path, monkeypatch).web_chat_enabled is False

    def test_only_a_real_true_turns_it_on(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"web_chat_enabled": True}).web_chat_enabled is True
        assert load(tmp_path, monkeypatch, {"web_chat_enabled": "yes"}).web_chat_enabled is False

    def test_the_default_port_loads_unchanged(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch).web_chat_port == get_default_config()["web_chat_port"]

    @pytest.mark.parametrize("bad", [0, 80, 70000, "8766x", None, True])
    def test_an_unusable_port_falls_back_to_the_default(self, tmp_path, monkeypatch, bad):
        default = get_default_config()["web_chat_port"]
        assert load(tmp_path, monkeypatch, {"web_chat_port": bad}).web_chat_port == default

    def test_a_valid_port_is_kept(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"web_chat_port": 9200}).web_chat_port == 9200

    def test_the_web_chat_and_phone_access_never_share_a_default_port(self):
        defaults = get_default_config()
        assert defaults["web_chat_port"] != defaults["remote_access_port"]

    def test_every_web_chat_setting_is_in_the_settings_window(self):
        from desktop_app.settings_window import CATEGORIES, FIELD_METADATA
        keys = {k for k in get_default_config() if k.startswith("web_chat_")}
        fields = {f.key: f for f in FIELD_METADATA}
        assert keys <= set(fields), sorted(keys - set(fields))
        assert {fields[k].category for k in keys} == {"web_chat"}
        assert "web_chat" in {key for key, _ in CATEGORIES}
