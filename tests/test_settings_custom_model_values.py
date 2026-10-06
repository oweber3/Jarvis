"""A model set in config.json that the Settings lists do not offer is shown and kept, never replaced."""
import json

import pytest


@pytest.fixture
def window_with(qapp, tmp_path, monkeypatch):
    def open_with(values):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"_config_version": 6, **values}))
        monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
        from desktop_app.settings_window import SettingsWindow
        return SettingsWindow(), cfg
    return open_with


@pytest.mark.unit
def test_an_unlisted_chat_model_is_shown_and_survives_saving(window_with):
    win, cfg = window_with({"ollama_chat_model": "my-custom-model:7b"})
    try:
        combo = win._widgets["ollama_chat_model"]
        assert combo.currentData() == "my-custom-model:7b"
        win._on_save()
        assert json.loads(cfg.read_text()).get("ollama_chat_model") == "my-custom-model:7b"
    finally:
        win.close()


@pytest.mark.unit
def test_gpt_oss_can_be_picked_for_chat_fast_and_tool_models():
    from desktop_app.settings_window import FIELD_METADATA
    for key in ("ollama_chat_model", "fast_model", "tool_model"):
        field = next(f for f in FIELD_METADATA if f.key == key)
        values = [value for value, _ in field.choices]
        assert "gpt-oss:20b" in values and "granite4.2:8b" in values, key
