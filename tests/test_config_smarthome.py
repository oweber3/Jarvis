"""The smarthome_* settings: off by default, typed, and never holding a secret (smarthome.spec.md)."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestSmartHomeSettings:
    def test_everything_is_off_by_default(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch)
        assert cfg.smarthome_enabled is False and cfg.smarthome_share_with_cloud is False
        assert cfg.smarthome_ha_url == "" and cfg.smarthome_mqtt_host == ""
        assert cfg.smarthome_ha_verify_tls is True and cfg.smarthome_mqtt_tls is False
        defaults = get_default_config()
        assert cfg.smarthome_mqtt_port == defaults["smarthome_mqtt_port"]
        assert cfg.smarthome_mqtt_discovery_prefix == defaults["smarthome_mqtt_discovery_prefix"]

    def test_values_are_loaded_and_trimmed(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {
            "smarthome_enabled": True, "smarthome_ha_url": " http://192.168.1.50:8123 ",
            "smarthome_mqtt_host": " 192.168.1.50 ", "smarthome_mqtt_port": 8883, "smarthome_mqtt_tls": True,
            "smarthome_mqtt_discovery_prefix": "/custom/", "smarthome_mqtt_hidden": ["mqtt:a", 3, " "],
            "smarthome_confirm_devices": ["ha:lock.front"], "smarthome_share_with_cloud": True})
        assert cfg.smarthome_enabled and cfg.smarthome_share_with_cloud and cfg.smarthome_mqtt_tls
        assert cfg.smarthome_ha_url == "http://192.168.1.50:8123" and cfg.smarthome_mqtt_host == "192.168.1.50"
        assert cfg.smarthome_mqtt_port == 8883 and cfg.smarthome_mqtt_discovery_prefix == "custom"
        assert cfg.smarthome_mqtt_hidden == ["mqtt:a"] and cfg.smarthome_confirm_devices == ["ha:lock.front"]

    @pytest.mark.parametrize("key,bad", [("smarthome_enabled", "yes"), ("smarthome_share_with_cloud", 1),
                                         ("smarthome_mqtt_port", "1883"), ("smarthome_mqtt_hidden", "mqtt:a")])
    def test_wrongly_typed_values_fall_back_to_the_safe_default(self, tmp_path, monkeypatch, key, bad):
        cfg = load(tmp_path, monkeypatch, {key: bad})
        assert getattr(cfg, key) == get_default_config()[key]

    def test_no_secret_is_a_setting(self):
        assert not [key for key in get_default_config() if key.startswith("smarthome_")
                    and any(word in key for word in ("token", "password", "secret"))]

    def test_the_secrets_are_credential_store_keys(self):
        from jarvis import credentials
        assert credentials.set_secret("smarthome.ha_token", "abc")
        assert credentials.get_secret("smarthome.ha_token") == "abc"
        assert credentials.set_secret("smarthome.mqtt_password", "pw")
        assert credentials.delete_secret("smarthome.mqtt_password")
        assert credentials.get_secret("smarthome.mqtt_password") == ""


@pytest.mark.unit
def test_tls_without_a_port_uses_the_tls_port(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, {"smarthome_mqtt_tls": True}).smarthome_mqtt_port == 8883
    assert load(tmp_path, monkeypatch, {"smarthome_mqtt_tls": True,
                                        "smarthome_mqtt_port": 1883}).smarthome_mqtt_port == 1883
