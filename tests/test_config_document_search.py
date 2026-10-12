"""Document search settings: off by default, nothing shared with the cloud, safe handling of bad values."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def write(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return path


@pytest.mark.unit
class TestDocumentSearchSettings:
    def test_off_and_private_by_default(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {})
        cfg = load_settings()
        defaults = get_default_config()
        assert cfg.document_search_enabled is False
        assert cfg.document_search_share_with_cloud is False
        assert cfg.document_search_folders == defaults["document_search_folders"] == ["Documents"]
        assert cfg.document_search_max_file_mb == defaults["document_search_max_file_mb"]
        assert cfg.document_search_rescan_minutes == defaults["document_search_rescan_minutes"]

    def test_only_a_real_true_opts_in(self, tmp_path, monkeypatch):
        for junk in ("true", 1, "yes", None):
            write(tmp_path, monkeypatch, {"document_search_enabled": junk,
                                          "document_search_share_with_cloud": junk})
            cfg = load_settings()
            assert cfg.document_search_enabled is False and cfg.document_search_share_with_cloud is False
        write(tmp_path, monkeypatch, {"document_search_enabled": True, "document_search_share_with_cloud": True})
        cfg = load_settings()
        assert cfg.document_search_enabled is True and cfg.document_search_share_with_cloud is True

    def test_folders_and_bounds(self, tmp_path, monkeypatch):
        write(tmp_path, monkeypatch, {"document_search_folders": ["Documents/Uni", "", 3, " Desktop "],
                                      "document_search_max_file_mb": 0, "document_search_rescan_minutes": 1})
        cfg = load_settings()
        assert cfg.document_search_folders == ["Documents/Uni", "Desktop"]
        assert cfg.document_search_max_file_mb >= 1
        assert cfg.document_search_rescan_minutes >= 5
        write(tmp_path, monkeypatch, {"document_search_folders": "Documents"})
        assert load_settings().document_search_folders == get_default_config()["document_search_folders"]

    def test_settings_window_shows_every_key(self):
        from desktop_app.settings_window import FIELD_METADATA
        keys = {meta.key for meta in FIELD_METADATA}
        for key in get_default_config():
            if key.startswith("document_search_"):
                assert key in keys, key
