"""The three thinking switches on the Settings window reach the code that reads them."""
import json

import pytest

KEYS = ("llm_thinking_enabled", "intent_judge_thinking_enabled", "dictation_thinking_enabled")


@pytest.mark.parametrize("key", KEYS)
def test_a_thinking_switch_set_in_config_is_loaded(key, tmp_path, monkeypatch):
    from jarvis import config
    path = tmp_path / "config.json"
    path.write_text(json.dumps({key: True}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    settings = config.load_settings()
    assert getattr(settings, key) is True
    assert all(getattr(settings, other) is False for other in KEYS if other != key)


def test_thinking_is_off_by_default(tmp_path, monkeypatch):
    from jarvis import config
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    settings = config.load_settings()
    assert [getattr(settings, key) for key in KEYS] == [False, False, False]
