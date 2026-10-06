"""The extensions_enabled and extensions_dir settings (``extensions/extensions.spec.md``, Configuration)."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def load(tmp_path, monkeypatch, values=None):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values or {}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
    return load_settings()


@pytest.mark.unit
class TestExtensionSettings:
    def test_no_extension_is_enabled_by_default(self, tmp_path, monkeypatch):
        defaults = get_default_config()
        assert defaults["extensions_enabled"] == [] and defaults["extensions_dir"] == ""
        cfg = load(tmp_path, monkeypatch)
        assert cfg.extensions_enabled == [] and cfg.extensions_dir == ""

    def test_names_and_folder_are_loaded(self, tmp_path, monkeypatch):
        cfg = load(tmp_path, monkeypatch, {"extensions_enabled": [" head ", "lights"],
                                           "extensions_dir": " ~/my_extensions "})
        assert cfg.extensions_enabled == ["head", "lights"]
        assert cfg.extensions_dir == "~/my_extensions"

    @pytest.mark.parametrize("bad, expected", [("head", []), (None, []), ({"head": True}, []),
                                               (["head", 5, "", None], ["head"])])
    def test_malformed_names_are_dropped(self, tmp_path, monkeypatch, bad, expected):
        assert load(tmp_path, monkeypatch, {"extensions_enabled": bad}).extensions_enabled == expected

    @pytest.mark.parametrize("bad", [None, 5, ["a"]])
    def test_a_malformed_folder_means_the_default(self, tmp_path, monkeypatch, bad):
        assert load(tmp_path, monkeypatch, {"extensions_dir": bad}).extensions_dir == ""
