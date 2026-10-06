"""Settings: speaker verification choice, barge-in toggle and deleting the voiceprint."""

import numpy as np
import pytest

from desktop_app.settings_window import FIELD_METADATA
from jarvis.config import get_default_config
from jarvis.listening import voiceprint

pytestmark = pytest.mark.unit


def _field(key):
    return next(f for f in FIELD_METADATA if f.key == key)


def test_speaker_verification_offers_off_soft_and_strict_with_off_as_default():
    field = _field("speaker_verification")
    assert field.field_type == "choice"
    assert [value for value, _ in field.choices] == ["off", "soft", "strict"]
    assert get_default_config()["speaker_verification"] == "off"


def test_barge_in_is_a_voice_input_toggle():
    field = _field("barge_in_enabled")
    assert field.field_type == "bool" and field.category == "voice_input"
    assert _field("speaker_verification").category == "voice_input"


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    config_dir = tmp_path / "jarvis"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_dir / "config.json"))
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from desktop_app.settings_window import SettingsWindow

    win = SettingsWindow()
    yield win
    win.close()
    app.processEvents()


def test_the_voice_page_says_whether_a_voice_is_enrolled(window):
    assert "not enrolled" in window._voice_status_label.text().lower()
    voiceprint.save_voiceprint(np.ones((1, 4), dtype=np.float32))
    window._refresh_voice_status()
    text = window._voice_status_label.text().lower()
    assert "enrolled" in text and "not enrolled" not in text


def test_the_delete_button_is_disabled_until_a_voice_is_enrolled(window):
    assert not window._delete_voiceprint_button.isEnabled()
    voiceprint.save_voiceprint(np.ones((1, 4), dtype=np.float32))
    window._refresh_voice_status()
    assert window._delete_voiceprint_button.isEnabled()


def test_confirming_the_delete_removes_the_voiceprint(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    voiceprint.save_voiceprint(np.ones((1, 4), dtype=np.float32))
    window._refresh_voice_status()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window._delete_voiceprint()
    assert not voiceprint.has_voiceprint()
    assert not window._delete_voiceprint_button.isEnabled()


def test_declining_the_delete_keeps_the_voiceprint(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    voiceprint.save_voiceprint(np.ones((1, 4), dtype=np.float32))
    window._refresh_voice_status()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    window._delete_voiceprint()
    assert voiceprint.has_voiceprint()


def test_the_voice_page_explains_how_to_enrol(window):
    assert "enrol_voice.py" in window._voice_hint_label.text()
