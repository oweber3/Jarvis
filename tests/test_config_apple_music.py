"""Apple Music settings: off by default, a store country that is either empty or two letters."""
import json

import pytest

from jarvis.config import get_default_config, load_settings


def write(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))


@pytest.mark.unit
def test_apple_music_is_off_by_default(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, {})
    cfg = load_settings()
    assert cfg.apple_music_enabled is False
    assert cfg.apple_music_country == ""
    defaults = get_default_config()
    assert defaults["apple_music_enabled"] is False and defaults["apple_music_country"] == ""


@pytest.mark.unit
def test_turning_it_on_is_read_back(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, {"apple_music_enabled": True, "apple_music_country": " gb "})
    cfg = load_settings()
    assert cfg.apple_music_enabled is True and cfg.apple_music_country == "GB"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["united kingdom", "g", 44, None, "1A"])
def test_a_bad_country_falls_back_to_the_windows_region(tmp_path, monkeypatch, value):
    write(tmp_path, monkeypatch, {"apple_music_country": value})
    assert load_settings().apple_music_country == ""


@pytest.mark.unit
def test_the_settings_window_describes_both_options():
    from desktop_app.settings_window import FIELD_METADATA
    names = {meta.key: meta for meta in FIELD_METADATA}
    assert "apple_music_enabled" in names and "apple_music_country" in names
    assert "Apple" in names["apple_music_enabled"].description
