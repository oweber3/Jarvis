"""The personality settings in the metadata-driven settings window: a choice and a custom text box."""
import json

import pytest

from desktop_app.settings_window import FIELD_METADATA
from jarvis.config import get_default_config
from jarvis.system_prompt import BUILTIN_PERSONALITIES, MAX_CUSTOM_CHARS


def field(key):
    return next(f for f in FIELD_METADATA if f.key == key)


@pytest.mark.unit
class TestFields:
    def test_personality_offers_every_builtin_and_custom_with_the_default(self):
        fm = field("personality")
        assert fm.field_type == "choice" and fm.category == "general"
        values = [value for value, _ in fm.choices]
        assert set(values) == set(BUILTIN_PERSONALITIES) | {"custom"}
        assert get_default_config()["personality"] in values

    def test_the_custom_text_is_a_text_box_shown_only_for_custom(self):
        fm = field("personality_custom_text")
        assert fm.field_type == "text"
        assert fm.shown_when(lambda key: "custom" if key == "personality" else None)
        assert not fm.shown_when(lambda key: "plain" if key == "personality" else None)
        assert str(MAX_CUSTOM_CHARS) in fm.description.replace(",", "")


@pytest.fixture
def open_window(qapp, tmp_path, monkeypatch):
    def open_with(values):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"_config_version": 6, **values}))
        monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
        from desktop_app.settings_window import SettingsWindow
        win = SettingsWindow()
        opened.append(win)
        return win, cfg
    opened = []
    yield open_with
    for win in opened:
        win.close()


@pytest.mark.unit
class TestWindow:
    def test_choosing_custom_reveals_the_text_box_and_saves_both(self, open_window):
        win, cfg = open_window({})
        assert not win.is_field_shown("personality_custom_text")
        combo = win._widgets["personality"]
        combo.setCurrentIndex(combo.findData("custom"))
        assert win.is_field_shown("personality_custom_text")
        win._widgets["personality_custom_text"].setPlainText("Speak like a calm\nship's captain.")
        win._on_save()
        saved = json.loads(cfg.read_text())
        assert saved["personality"] == "custom"
        assert saved["personality_custom_text"] == "Speak like a calm\nship's captain."

    def test_saved_text_is_shown_again_and_kept_unchanged(self, open_window):
        win, cfg = open_window({"personality": "custom", "personality_custom_text": "Line one.\nLine two."})
        assert win._widgets["personality_custom_text"].toPlainText() == "Line one.\nLine two."
        win._on_save()
        assert json.loads(cfg.read_text())["personality_custom_text"] == "Line one.\nLine two."

    def test_the_default_personality_is_not_written(self, open_window):
        win, cfg = open_window({"personality": "plain"})
        combo = win._widgets["personality"]
        combo.setCurrentIndex(combo.findData("butler"))
        win._on_save()
        assert "personality" not in json.loads(cfg.read_text())

    def test_a_voice_switch_while_the_window_is_open_survives_saving(self, open_window):
        from jarvis.config import update_config_values
        win, cfg = open_window({})
        update_config_values({"personality": "friendly"})  # "be friendly" said while Settings is open
        win._on_save()
        assert json.loads(cfg.read_text())["personality"] == "friendly"
