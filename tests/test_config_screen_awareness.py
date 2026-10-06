"""Screen awareness setting: on by default, a single switch that turns it off, shown in Settings."""
import json

import pytest

from jarvis.config import get_default_config, load_settings

pytestmark = pytest.mark.unit


def write(tmp_path, monkeypatch, values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return path


def test_screen_awareness_is_on_by_default(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, {})
    assert load_settings().screen_awareness_enabled is get_default_config()["screen_awareness_enabled"] is True


@pytest.mark.parametrize("value", [False, "false", 0])
def test_anything_but_true_turns_it_off(tmp_path, monkeypatch, value):
    write(tmp_path, monkeypatch, {"screen_awareness_enabled": value})
    assert load_settings().screen_awareness_enabled is False


def test_the_switch_is_in_settings():
    from desktop_app.settings_window import FIELD_METADATA
    keys = {getattr(field, "key", None) for field in FIELD_METADATA}
    assert "screen_awareness_enabled" in keys
