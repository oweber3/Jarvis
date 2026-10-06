"""API keys in Settings: shown only as stored or not, saved to the credential store, never to config.json."""
import json

import pytest

import jarvis.credentials as credentials
from desktop_app.settings_window import FIELD_METADATA

SECRET_KEYS = ("llm_api_key", "embedding_api_key", "brave_search_api_key")


def field(key):
    return next(f for f in FIELD_METADATA if f.key == key)


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_config_version": 6, "brave_search_api_key": "plain-brave"}))
    monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
    credentials.set_secret("llm_api_key", "sk-stored-123")
    from desktop_app.settings_window import SettingsWindow
    win = SettingsWindow()
    yield win, cfg
    win.close()


def visible_text(widget):
    from PyQt6.QtWidgets import QLabel, QLineEdit
    texts = [w.text() for w in widget.findChildren(QLabel)] + [w.text() for w in widget.findChildren(QLineEdit)]
    texts += [w.placeholderText() for w in widget.findChildren(QLineEdit)]
    return " ".join(texts)


@pytest.mark.unit
class TestSecretFields:
    def test_every_api_key_is_a_secret_field(self):
        for key in SECRET_KEYS:
            fm = field(key)
            assert fm.field_type == "secret", key
            assert "credential manager" in fm.description.lower()

    def test_a_stored_key_is_reported_but_never_shown(self, window):
        win, _ = window
        text = visible_text(win._widgets["llm_api_key"])
        assert "sk-stored-123" not in text and "stored" in text.lower()
        assert "stored" not in visible_text(win._widgets["embedding_api_key"]).lower().replace("to store", "")

    def test_opening_settings_moves_a_plaintext_key_out_of_config(self, window):
        win, cfg = window
        assert "brave_search_api_key" not in json.loads(cfg.read_text())
        assert credentials.get_secret("brave_search_api_key") == "plain-brave"
        assert "plain-brave" not in visible_text(win._widgets["brave_search_api_key"])

    def test_saving_a_new_key_stores_it_and_keeps_it_out_of_config(self, window):
        win, cfg = window
        win._widgets["embedding_api_key"]._edit.setText("sk-new-456")
        win._on_save()
        assert credentials.get_secret("embedding_api_key") == "sk-new-456"
        assert "sk-new-456" not in cfg.read_text() and "embedding_api_key" not in json.loads(cfg.read_text())
        assert credentials.get_secret("llm_api_key") == "sk-stored-123"

    def test_remove_deletes_the_stored_key_on_save(self, window):
        win, _ = window
        win._widgets["llm_api_key"]._remove.click()
        assert credentials.get_secret("llm_api_key") == "sk-stored-123"
        win._on_save()
        assert credentials.get_secret("llm_api_key") == ""

    def test_reset_to_defaults_does_not_delete_stored_keys(self, window, monkeypatch):
        from desktop_app.settings_window import QMessageBox
        win, _ = window
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.question",
                            lambda *a, **k: QMessageBox.StandardButton.Yes)
        win._widgets["llm_api_key"]._edit.setText("typed")
        win._on_reset()
        assert win._widgets["llm_api_key"]._edit.text() == ""
        win._on_save()
        assert credentials.get_secret("llm_api_key") == "sk-stored-123"

    def test_a_store_failure_is_reported_and_nothing_reaches_config(self, window, monkeypatch):
        win, cfg = window
        warnings = []
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.warning", lambda *a, **k: warnings.append(a))
        monkeypatch.setattr(credentials, "set_secret", lambda key, value: False)
        win._widgets["embedding_api_key"]._edit.setText("sk-lost")
        win._on_save()
        assert warnings and "sk-lost" not in cfg.read_text()


class _NoStore:
    """A machine without a usable credential store."""

    def get_password(self, *_a):
        raise RuntimeError("no backend")

    def set_password(self, *_a):
        raise RuntimeError("no backend")

    def delete_password(self, *_a):
        raise RuntimeError("no backend")


@pytest.mark.unit
def test_without_a_credential_store_saving_settings_keeps_the_key_in_config(qapp, tmp_path, monkeypatch):
    """The key could not move to the store, so config.json is the only copy: Save must not drop it."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_config_version": 6, "brave_search_api_key": "plain-brave"}))
    monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.warning", lambda *a, **k: None)
    monkeypatch.setattr(credentials, "_backend", _NoStore())
    from desktop_app.settings_window import SettingsWindow
    win = SettingsWindow()
    try:
        win._on_save()
    finally:
        win.close()
    assert json.loads(cfg.read_text()).get("brave_search_api_key") == "plain-brave"
