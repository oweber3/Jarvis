"""The roku_host setting: empty by default, loaded as text, and preserved."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestRokuHostSetting:
    def test_no_tv_is_configured_by_default(self, tmp_path, monkeypatch):
        assert get_default_config()["roku_host"] == ""
        assert load(tmp_path, monkeypatch).roku_host == ""

    def test_the_host_is_loaded_and_trimmed(self, tmp_path, monkeypatch):
        assert load(tmp_path, monkeypatch, {"roku_host": " 192.168.1.50 "}).roku_host == "192.168.1.50"

    @pytest.mark.parametrize("bad", [None, 5, ["192.168.1.50"], {"a": 1}])
    def test_a_wrongly_typed_value_means_no_tv(self, tmp_path, monkeypatch, bad):
        assert load(tmp_path, monkeypatch, {"roku_host": bad}).roku_host == ""

    def test_no_home_network_address_is_built_into_the_defaults_or_the_code(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        address = re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")
        documented_examples = {"192.168.1.50"}
        for path in [*(root / "src").rglob("*.py"), *(root / "src").rglob("*.json")]:
            found = set(address.findall(path.read_text(encoding="utf-8", errors="ignore")))
            assert not {a for a in found if not a.endswith(".0")} - documented_examples, path
