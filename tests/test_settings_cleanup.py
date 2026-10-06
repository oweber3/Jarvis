"""Settings window: dead settings gone, MLX offered only on macOS, reply pages first, config.json note."""
import json

import pytest

DEAD_KEYS = ("llm_profile_select_timeout_sec", "voice_block_seconds")


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
def test_dead_settings_have_no_rows_and_no_config_field():
    import dataclasses

    from desktop_app.settings_window import FIELD_METADATA
    from jarvis.config import Settings, get_default_config

    shown = {f.key for f in FIELD_METADATA}
    fields = {f.name for f in dataclasses.fields(Settings)}
    for key in DEAD_KEYS:
        assert key not in shown
        assert key not in fields
        assert key not in get_default_config()


@pytest.mark.unit
def test_config_file_still_holding_dead_keys_loads(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_config_version": 6, **{k: 12.0 for k in DEAD_KEYS}}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
    from jarvis.config import load_settings

    settings = load_settings()
    for key in DEAD_KEYS:
        assert not hasattr(settings, key)


@pytest.mark.unit
@pytest.mark.parametrize("platform,offers_mlx", [("win32", False), ("linux", False), ("darwin", True)])
def test_mlx_is_offered_only_on_macos(monkeypatch, platform, offers_mlx):
    monkeypatch.setattr("sys.platform", platform)
    from desktop_app.settings_window import _build_field_metadata

    field = next(f for f in _build_field_metadata() if f.key == "whisper_backend")
    values = [value for value, _ in field.choices]
    assert ("mlx" in values) is offers_mlx
    assert "auto" in values and "faster-whisper" in values


@pytest.mark.unit
def test_a_hand_set_mlx_backend_is_shown_and_kept_on_windows(window_with, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    import desktop_app.settings_window as sw

    monkeypatch.setattr(sw, "FIELD_METADATA", sw._build_field_metadata())
    win, cfg = window_with({"whisper_backend": "mlx"})
    try:
        assert win._widgets["whisper_backend"].currentData() == "mlx"
        win._on_save()
        assert json.loads(cfg.read_text()).get("whisper_backend") == "mlx"
    finally:
        win.close()


@pytest.mark.unit
def test_reply_pages_lead_the_sidebar_in_order():
    from desktop_app.settings_window import CATEGORIES

    keys = [k for k, _ in CATEGORIES]
    assert keys[:4] == ["reply", "codex", "claude", "llm"]
    rest = [k for k in keys if k not in ("reply", "codex", "claude")]
    assert rest[:3] == ["llm", "llm_provider", "tts"]


@pytest.mark.unit
def test_windows_page_names_the_config_only_features_and_their_specs(window_with):
    from desktop_app.settings_window import CONFIG_ONLY_WINDOWS_KEYS

    win, _cfg = window_with({})
    try:
        text = win._windows_config_note.text()
        for key in CONFIG_ONLY_WINDOWS_KEYS:
            assert key in text
        assert "config.json" in text
        for spec in (
            "src/jarvis/platform/windows/workspaces.spec.md",
            "src/jarvis/routines/routines.spec.md",
            "src/jarvis/platform/windows/apps_paths.spec.md",
        ):
            assert spec in text
    finally:
        win.close()
