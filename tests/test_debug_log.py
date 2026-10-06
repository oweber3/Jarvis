"""debug_log decides whether to print from the voice_debug setting, without re-entering itself."""
import json

import pytest


class _NoStore:
    def get_password(self, *_a):
        raise RuntimeError("no credential store")


@pytest.mark.unit
def test_reading_the_config_when_the_credential_store_fails_does_not_recurse(tmp_path, monkeypatch, capsys):
    """Loading settings logs a failed credential read, and logging reads settings: that must not loop."""
    from jarvis import config, credentials, debug
    path = tmp_path / "config.json"
    path.write_text(json.dumps({}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    monkeypatch.setenv("JARVIS_VOICE_DEBUG", "1")
    monkeypatch.setattr(credentials, "_backend", _NoStore())
    monkeypatch.setattr(debug, "_cached_voice_debug", None)
    monkeypatch.setattr(debug, "_last_check_time", 0.0)
    loads = []
    real = config.load_settings
    monkeypatch.setattr(debug, "load_settings", lambda: loads.append(1) or real())

    config.load_settings()
    assert len(loads) <= 1
    debug.debug_log("still logging", "test")
    assert "still logging" in capsys.readouterr().err
