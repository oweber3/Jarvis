"""Reply-mode, Codex and Claude settings appear in the metadata-driven settings window."""
import pytest

from desktop_app.settings_window import CATEGORIES, FIELD_METADATA
from jarvis.config import get_default_config

MODE_KEYS = {"reply_mode", "codex_enabled", "claude_enabled"}


def field(key):
    return next(f for f in FIELD_METADATA if f.key == key)


@pytest.mark.unit
class TestReplyModeSettingsFields:
    def test_every_bridge_setting_is_exposed(self):
        exposed = {fm.key for fm in FIELD_METADATA}
        keys = {k for k in get_default_config() if k.startswith(("codex_", "claude_"))} | MODE_KEYS
        assert keys <= exposed, sorted(keys - exposed)

    def test_no_visible_chat_controls_remain(self):
        for fm in FIELD_METADATA:
            assert not fm.key.startswith("codex_desktop_"), fm.key
            text = (fm.label + " " + fm.description).lower()
            if fm.category == "codex":
                assert "bind" not in text and "dedicated chat" not in text and "model picker" not in text

    def test_mode_choice_and_permissions_have_their_own_page_and_each_bridge_its_own(self):
        assert {"reply", "codex", "claude"} <= {key for key, _ in CATEGORIES}
        for fm in FIELD_METADATA:
            if fm.key in MODE_KEYS:
                assert fm.category == "reply", fm.key
            elif fm.key.startswith("codex_"):
                assert fm.category == "codex", fm.key
            elif fm.key.startswith("claude_"):
                assert fm.category == "claude", fm.key

    def test_reply_mode_is_a_choice_between_local_codex_and_claude(self):
        fm = field("reply_mode")
        assert fm.field_type == "choice"
        assert {value for value, _ in fm.choices} == {"local", "codex", "claude"}

    def test_the_mode_rows_state_the_cloud_disclosure_and_the_local_default(self):
        text = " ".join((field(k).description + " " + " ".join(label for _, label in (field(k).choices or [])))
                        for k in MODE_KEYS).lower()
        assert "openai" in text and "anthropic" in text and "local" in text and "sign" in text
        assert "allow" in field("claude_enabled").label.lower() and field("claude_enabled").field_type == "bool"

    @pytest.mark.parametrize("key", ["codex_reasoning_effort", "claude_effort"])
    def test_the_default_effort_is_one_of_the_offered_choices(self, key):
        assert get_default_config()[key] in {value for value, _ in field(key).choices}

    @pytest.mark.parametrize("key", ["codex_share_long_term_memory", "claude_share_long_term_memory"])
    def test_privacy_rows_say_what_is_shared(self, key):
        description = field(key).description.lower()
        assert "off" in description or "never" in description


@pytest.fixture
def open_window(qapp, tmp_path, monkeypatch):
    import json
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_config_version": 6, "claude_enabled": True, "reply_mode": "claude",
                               "activity_log_enabled": True}))
    monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
    from desktop_app.settings_window import SettingsWindow
    win = SettingsWindow()
    yield win, cfg
    win.close()


@pytest.mark.unit
class TestSaveKeepsChangesMadeElsewhere:
    """Voice and the tray write settings while the window is open; Save writes only what the user changed."""

    def test_a_voice_switch_to_local_survives_saving_settings(self, open_window):
        import json
        from jarvis.config import update_config_values
        win, cfg = open_window
        update_config_values({"reply_mode": "local", "activity_log_paused": True})  # "go local", tray pause
        win._on_save()
        saved = json.loads(cfg.read_text())
        assert saved.get("reply_mode", "local") == "local"
        assert saved.get("activity_log_paused") is True

    def test_a_field_changed_in_the_window_is_still_saved(self, open_window):
        import json
        from PyQt6.QtWidgets import QComboBox
        win, cfg = open_window
        combo = win._widgets["reply_mode"]
        combo = combo if isinstance(combo, QComboBox) else combo.findChild(QComboBox)
        combo.setCurrentIndex(combo.findData("codex"))
        win._on_save()
        assert json.loads(cfg.read_text())["reply_mode"] == "codex"


@pytest.mark.unit
class TestBridgeModelSelectors:
    """Each cloud mode's default model is picked from a list; a hand-set model is still shown and kept."""

    @pytest.mark.parametrize("key", ["codex_model", "claude_model"])
    def test_the_model_is_a_dropdown_offering_the_default(self, key):
        fm = field(key)
        assert fm.field_type == "choice"
        assert get_default_config()[key] in {value for value, _ in fm.choices}

    @pytest.mark.parametrize("key", ["codex_model", "claude_model"])
    def test_the_dropdown_offers_more_than_one_model(self, key):
        assert len({value for value, _ in field(key).choices}) > 1

    @pytest.mark.parametrize("key, custom", [("codex_model", "gpt-unlisted-1"),
                                             ("claude_model", "claude-unlisted-1")])
    def test_an_unlisted_model_is_shown_and_survives_saving(self, qapp, tmp_path, monkeypatch, key, custom):
        import json
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"_config_version": 6, key: custom}))
        monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
        from desktop_app.settings_window import SettingsWindow
        win = SettingsWindow()
        try:
            assert win._widgets[key].currentData() == custom
            win._on_save()
            assert json.loads(cfg.read_text()).get(key) == custom
        finally:
            win.close()
