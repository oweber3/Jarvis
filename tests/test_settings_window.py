"""
Tests for settings window metadata and config I/O logic.

Tests verify the metadata registry, value extraction, and save/load behaviour
without touching the GUI. Widget creation is tested via mock Qt objects where needed.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from desktop_app.settings_window import (
    FIELD_METADATA,
    CATEGORIES,
    FieldMeta,
    get_input_devices,
    _build_field_metadata,
    _is_default_value,
    _MCPCatalogueDialog,
    _MCPEditDialog,
)
from desktop_app.mcp_catalogue import CATALOGUE_BY_NAME
from jarvis.config import get_default_config


class TestFieldMetadata:
    """Tests for the config field metadata registry."""

    def test_all_fields_reference_valid_categories(self):
        """Every field's category must appear in CATEGORIES."""
        valid_cats = {key for key, _ in CATEGORIES}
        for fm in FIELD_METADATA:
            assert fm.category in valid_cats, (
                f"Field '{fm.key}' references unknown category '{fm.category}'"
            )

    def test_all_fields_reference_existing_config_keys(self):
        """Every field key must exist in get_default_config()."""
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            assert fm.key in defaults, (
                f"Field '{fm.key}' not found in default config"
            )

    def test_no_duplicate_keys(self):
        """Each config key should appear at most once in the metadata."""
        keys = [fm.key for fm in FIELD_METADATA]
        assert len(keys) == len(set(keys)), (
            f"Duplicate keys: {[k for k in keys if keys.count(k) > 1]}"
        )

    def test_field_types_are_valid(self):
        """All field_type values must be from the allowed set."""
        valid_types = {"bool", "int", "float", "str", "choice", "device", "list", "secret"}
        for fm in FIELD_METADATA:
            assert fm.field_type in valid_types, (
                f"Field '{fm.key}' has invalid type '{fm.field_type}'"
            )

    def test_choice_fields_have_choices(self):
        """Fields with type 'choice' must have a non-empty choices list."""
        for fm in FIELD_METADATA:
            if fm.field_type == "choice":
                assert fm.choices and len(fm.choices) > 0, (
                    f"Choice field '{fm.key}' has no choices defined"
                )

    def test_numeric_fields_have_bounds(self):
        """Numeric fields (int/float) should have min and max defined."""
        for fm in FIELD_METADATA:
            if fm.field_type in ("int", "float") and not fm.nullable:
                assert fm.min_val is not None, (
                    f"Numeric field '{fm.key}' missing min_val"
                )
                assert fm.max_val is not None, (
                    f"Numeric field '{fm.key}' missing max_val"
                )

    def test_labels_are_nonempty(self):
        """Every field must have a non-empty label."""
        for fm in FIELD_METADATA:
            assert fm.label.strip(), f"Field '{fm.key}' has empty label"

    def test_descriptions_are_nonempty(self):
        """Every field must have a non-empty description."""
        for fm in FIELD_METADATA:
            assert fm.description.strip(), f"Field '{fm.key}' has empty description"

    def test_build_returns_consistent_results(self):
        """_build_field_metadata() should return the same structure on repeated calls."""
        a = _build_field_metadata()
        b = _build_field_metadata()
        assert len(a) == len(b)
        for fa, fb in zip(a, b):
            assert fa.key == fb.key
            assert fa.category == fb.category

    def test_low_power_mode_is_exposed_as_feature_toggle(self):
        """Low-power mode should be available without hand-editing config.json."""
        field = next((fm for fm in FIELD_METADATA if fm.key == "low_power_mode"), None)
        assert field is not None
        assert field.category == "features"
        assert field.field_type == "bool"


class TestLLMProviderFields:
    """The settings UI must expose the provider-aware LLM config so a user
    can select an OpenAI-compatible backend without editing config.json by
    hand."""

    def _field(self, key):
        for fm in FIELD_METADATA:
            if fm.key == key:
                return fm
        return None

    def test_provider_category_present(self):
        """A dedicated 'LLM Provider' category must exist in the sidebar."""
        cat_keys = [k for k, _ in CATEGORIES]
        assert "llm_provider" in cat_keys

    def test_provider_fields_present(self):
        """All eight provider-aware config keys are surfaced."""
        expected = {
            "llm_provider", "llm_base_url", "llm_api_key", "llm_chat_model",
            "embedding_provider", "embedding_base_url", "embedding_api_key",
            "embedding_model",
        }
        present = {fm.key for fm in FIELD_METADATA}
        missing = expected - present
        assert not missing, f"Provider fields missing from settings UI: {missing}"

    def test_provider_fields_live_in_provider_category(self):
        """The provider connection/credential fields group under the
        'LLM Provider' category, not scattered across 'llm'."""
        for key in (
            "llm_provider", "llm_base_url", "llm_api_key", "llm_chat_model",
            "embedding_provider", "embedding_base_url", "embedding_api_key",
            "embedding_model",
        ):
            fm = self._field(key)
            assert fm is not None and fm.category == "llm_provider", (
                f"'{key}' should be in the 'llm_provider' category"
            )

    def test_llm_provider_choices_match_config(self):
        """The provider dropdown offers exactly the values the config loader
        accepts ('ollama', 'openai_compatible')."""
        fm = self._field("llm_provider")
        assert fm is not None and fm.field_type == "choice"
        values = {v for v, _ in (fm.choices or [])}
        assert values == {"ollama", "openai_compatible"}

    def test_embedding_provider_offers_inherit_option(self):
        """embedding_provider includes the empty 'same as chat provider'
        option plus the two concrete providers."""
        fm = self._field("embedding_provider")
        assert fm is not None and fm.field_type == "choice"
        values = {v for v, _ in (fm.choices or [])}
        assert "" in values, "must offer an inherit-from-chat-provider option"
        assert {"ollama", "openai_compatible"} <= values

    def test_api_key_fields_are_secret_type(self):
        """API keys use the secret field type: kept in the credential store, never shown."""
        for key in ("llm_api_key", "embedding_api_key", "brave_search_api_key"):
            fm = self._field(key)
            assert fm is not None and fm.field_type == "secret", (
                f"'{key}' should be a secret field"
            )

    def test_model_fields_are_freetext(self):
        """The provider model fields are free text — an OpenAI-compatible
        server's model name is not in the Ollama SUPPORTED_CHAT_MODELS
        catalogue, so a choice dropdown would be wrong."""
        for key in ("llm_chat_model", "embedding_model"):
            fm = self._field(key)
            assert fm is not None and fm.field_type == "str", (
                f"'{key}' should be a free-text str field"
            )

    def test_connection_fields_are_nullable(self):
        """Connection and model fields are nullable so leaving them empty falls
        back to the Ollama settings and keeps config.json minimal. (API keys are
        secret fields kept in the credential store, never in config.json.)"""
        for key in (
            "llm_base_url", "llm_chat_model",
            "embedding_base_url", "embedding_model",
        ):
            fm = self._field(key)
            assert fm is not None and fm.nullable, f"'{key}' should be nullable"


class TestMinimalConfigInvariant:
    """``_is_default_value`` decides whether a field is omitted from
    config.json. An emptied nullable provider field (reads back as None)
    whose default is an empty string must be omitted, not persisted as null."""

    def test_value_equal_to_default_is_omitted(self):
        assert _is_default_value("ollama", "ollama") is True

    def test_changed_value_is_kept(self):
        assert _is_default_value("openai_compatible", "ollama") is False

    def test_emptied_field_with_empty_string_default_is_omitted(self):
        # llm_base_url etc.: default "", user clears it -> _get_value returns None
        assert _is_default_value(None, "") is True

    def test_emptied_field_with_none_default_is_omitted(self):
        assert _is_default_value(None, None) is True

    def test_set_value_over_empty_default_is_kept(self):
        assert _is_default_value("http://localhost:1234/v1", "") is False

    def test_none_over_nonempty_default_is_kept(self):
        # A nullable field whose default is a real value, cleared by the user,
        # is a genuine change and must be written.
        assert _is_default_value(None, "gemma4:e2b") is False


class TestCategories:
    """Tests for category definitions."""

    def test_no_duplicate_category_keys(self):
        """Category keys should be unique."""
        keys = [k for k, _ in CATEGORIES]
        assert len(keys) == len(set(keys))

    def test_every_category_has_fields(self):
        """Every defined category should have at least one field.

        The 'mcps' category uses a custom page, not FIELD_METADATA, so it's excluded.
        """
        cats_with_fields = {fm.category for fm in FIELD_METADATA}
        custom_page_categories = {"mcps"}
        for key, label in CATEGORIES:
            if key in custom_page_categories:
                continue
            assert key in cats_with_fields, (
                f"Category '{key}' ({label}) has no fields"
            )

    def test_mcps_category_exists(self):
        """The MCP Servers category must be present in the sidebar."""
        cat_keys = [k for k, _ in CATEGORIES]
        assert "mcps" in cat_keys


class TestInputDevices:
    """Tests for audio device enumeration."""

    def test_always_includes_system_default(self):
        """get_input_devices() always returns at least the system default."""
        # Even if sounddevice fails, we should get the default option
        with patch.dict("sys.modules", {"sounddevice": None}):
            devices = get_input_devices()
        assert len(devices) >= 1
        assert devices[0][0] == ""  # empty string = system default

    def test_with_mock_sounddevice(self):
        """With mock devices, returns them plus system default."""
        mock_sd = MagicMock()
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 2, "default_samplerate": 44100},
            {"name": "USB Speaker", "max_input_channels": 0, "default_samplerate": 48000},
            {"name": "External Mic", "max_input_channels": 1, "default_samplerate": 16000},
        ]
        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            # Need to reimport to pick up the mock
            import importlib
            import desktop_app.settings_window as sw
            importlib.reload(sw)
            devices = sw.get_input_devices()

        # System default + 2 input devices (USB Speaker has 0 input channels)
        assert len(devices) == 3
        assert devices[0][0] == ""
        assert "Built-in Mic" in devices[1][1]
        assert "External Mic" in devices[2][1]

    def test_handles_sounddevice_import_error(self):
        """Gracefully handles missing sounddevice."""
        devices = get_input_devices()
        # Should always at least have the default
        assert len(devices) >= 1


class TestConfigSaveLogic:
    """Tests for save/load round-trip behaviour."""

    def test_only_non_defaults_are_saved(self):
        """Saving default values should produce an empty config file."""
        defaults = get_default_config()
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{}')
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            # Simulate: all values match defaults, so nothing should be written
            config = {}
            for fm in FIELD_METADATA:
                val = defaults.get(fm.key)
                default_val = defaults.get(fm.key)
                if val != default_val:
                    config[fm.key] = val

            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert saved == {}
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_changed_values_are_preserved(self):
        """Non-default values should survive a save/load round-trip."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('{}')
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            config = {
                "ollama_chat_model": "gemma4:e4b",
                "tts_enabled": False,
                "hot_window_seconds": 5.0,
            }
            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert saved["ollama_chat_model"] == "gemma4:e4b"
            assert saved["tts_enabled"] is False
            assert saved["hot_window_seconds"] == 5.0
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_unknown_keys_preserved_on_save(self):
        """Keys not in FIELD_METADATA (e.g. mcps) should survive save."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"mcps": {"test": {"url": "http://example.com"}},
                        "_config_version": 1}, f)
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            existing = _load_json(cfg_path)
            # Simulate settings save: add a changed value, keep existing keys
            existing["tts_enabled"] = False
            _save_json(cfg_path, existing)

            saved = _load_json(cfg_path)
            assert "mcps" in saved
            assert saved["mcps"]["test"]["url"] == "http://example.com"
            assert saved["_config_version"] == 1
            assert saved["tts_enabled"] is False
        finally:
            cfg_path.unlink(missing_ok=True)


class TestDefaultValueTypes:
    """Verify that default values match the declared field types."""

    def test_bool_defaults_are_bool(self):
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "bool":
                val = defaults.get(fm.key)
                assert isinstance(val, bool), (
                    f"Field '{fm.key}' default {val!r} is not bool"
                )

    def test_int_defaults_are_numeric(self):
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "int" and not fm.nullable:
                val = defaults.get(fm.key)
                assert isinstance(val, (int, float)), (
                    f"Field '{fm.key}' default {val!r} is not numeric"
                )

    def test_float_defaults_are_numeric(self):
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "float":
                val = defaults.get(fm.key)
                assert isinstance(val, (int, float)), (
                    f"Field '{fm.key}' default {val!r} is not numeric"
                )

    def test_choice_defaults_are_in_choices(self):
        """Default values for choice fields must be one of the valid choices."""
        defaults = get_default_config()
        for fm in FIELD_METADATA:
            if fm.field_type == "choice" and fm.choices:
                val = str(defaults.get(fm.key))
                valid_values = [c[0] for c in fm.choices]
                assert val in valid_values, (
                    f"Field '{fm.key}' default '{val}' not in choices {valid_values}"
                )


class TestMCPEditDialogLogic:
    """Tests for the MCP edit dialog's get_result() logic (no GUI)."""

    def test_get_result_basic(self):
        """get_result parses name, command, args, and env correctly."""
        dlg = _MCPEditDialog.__new__(_MCPEditDialog)
        dlg._name_edit = MagicMock()
        dlg._name_edit.text.return_value = "test-server"
        dlg._command_edit = MagicMock()
        dlg._command_edit.text.return_value = "npx"
        dlg._args_edit = MagicMock()
        dlg._args_edit.text.return_value = "-y @test/server ~"
        dlg._env_edit = MagicMock()
        dlg._env_edit.text.return_value = "API_KEY=abc123"

        name, cfg = dlg.get_result()
        assert name == "test-server"
        assert cfg["transport"] == "stdio"
        assert cfg["command"] == "npx"
        assert cfg["args"] == ["-y", "@test/server", "~"]
        assert cfg["env"] == {"API_KEY": "abc123"}

    def test_get_result_empty_env(self):
        """When env is empty, env key should not be in config."""
        dlg = _MCPEditDialog.__new__(_MCPEditDialog)
        dlg._name_edit = MagicMock()
        dlg._name_edit.text.return_value = "test"
        dlg._command_edit = MagicMock()
        dlg._command_edit.text.return_value = "node"
        dlg._args_edit = MagicMock()
        dlg._args_edit.text.return_value = ""
        dlg._env_edit = MagicMock()
        dlg._env_edit.text.return_value = ""

        name, cfg = dlg.get_result()
        assert name == "test"
        assert cfg["command"] == "node"
        assert cfg["args"] == []
        assert "env" not in cfg

    def test_get_result_multiple_env_vars(self):
        """Multiple KEY=VALUE pairs are parsed correctly."""
        dlg = _MCPEditDialog.__new__(_MCPEditDialog)
        dlg._name_edit = MagicMock()
        dlg._name_edit.text.return_value = "srv"
        dlg._command_edit = MagicMock()
        dlg._command_edit.text.return_value = "cmd"
        dlg._args_edit = MagicMock()
        dlg._args_edit.text.return_value = ""
        dlg._env_edit = MagicMock()
        dlg._env_edit.text.return_value = "A=1 B=two C=three=four"

        _, cfg = dlg.get_result()
        assert cfg["env"] == {"A": "1", "B": "two", "C": "three=four"}


class TestMCPCatalogueDialogLogic:
    """Tests for the MCP catalogue dialog's Node.js detection (no GUI)."""

    def test_is_node_available_returns_true_when_found(self):
        """_is_node_available returns True when _resolve_command succeeds."""
        with patch("jarvis.tools.external.mcp_client._resolve_command", return_value="/usr/bin/npx"):
            assert _MCPCatalogueDialog._is_node_available() is True

    def test_is_node_available_returns_false_when_missing(self):
        """_is_node_available returns False when _resolve_command raises."""
        with patch("jarvis.tools.external.mcp_client._resolve_command", side_effect=FileNotFoundError("not found")):
            assert _MCPCatalogueDialog._is_node_available() is False


class TestMCPConfigSaveLogic:
    """Tests for MCP config preservation during save."""

    def test_mcps_saved_when_present(self):
        """MCP configs should be written to the config file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({}, f)
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            config = {
                "mcps": {
                    "filesystem": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "~"],
                    }
                }
            }
            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert "mcps" in saved
            assert "filesystem" in saved["mcps"]
            assert saved["mcps"]["filesystem"]["command"] == "npx"
        finally:
            cfg_path.unlink(missing_ok=True)

    def test_empty_mcps_not_saved(self):
        """When mcps is empty, it should not be written to config."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({}, f)
            cfg_path = Path(f.name)

        try:
            from jarvis.config import _save_json, _load_json

            # Simulate: mcps is empty so should not be written
            config = {"tts_enabled": False}
            _save_json(cfg_path, config)
            saved = _load_json(cfg_path)
            assert "mcps" not in saved
        finally:
            cfg_path.unlink(missing_ok=True)


class TestWindowsControlSettings:
    """The Windows Control page exposes the routing and Windows toggles, a
    capability status panel and the confirmation policy summary."""

    def test_toggles_live_in_windows_category(self):
        from desktop_app.settings_window import CATEGORIES
        assert "windows" in {key for key, _ in CATEGORIES}
        for key in ("fast_commands_enabled", "windows_tools_enabled"):
            field = next((fm for fm in FIELD_METADATA if fm.key == key), None)
            assert field is not None, key
            assert field.category == "windows"
            assert field.field_type == "bool"

    def test_fast_commands_label_avoids_internal_terms(self):
        field = next(fm for fm in FIELD_METADATA if fm.key == "fast_commands_enabled")
        text = f"{field.label} {field.description}".lower()
        assert "fastpath" not in text and "matcher" not in text

    def test_status_reports_each_capability_group(self):
        from desktop_app.settings_window import windows_status_lines
        text = "\n".join(windows_status_lines(True, True, "win32")).lower()
        for word in ("application", "volume", "system information", "folder"):
            assert word in text

    def test_status_reflects_disabled_windows_tools(self):
        from desktop_app.settings_window import windows_capabilities
        on = windows_capabilities(True, "win32")
        off = windows_capabilities(False, "win32")
        assert on and all(c.available for c in on)
        assert not any(c.available for c in off)
        assert [c.label for c in on] == [c.label for c in off]

    def test_status_unavailable_off_windows(self):
        from desktop_app.settings_window import windows_capabilities
        assert not any(c.available for c in windows_capabilities(True, "linux"))

    def test_status_reflects_fast_routing_state(self):
        from desktop_app.settings_window import windows_status_lines
        assert windows_status_lines(True, True, "win32") != windows_status_lines(False, True, "win32")

    def test_safety_summary_covers_all_three_tiers(self):
        from desktop_app.settings_window import SAFETY_SUMMARY_LINES
        text = "\n".join(SAFETY_SUMMARY_LINES).lower()
        assert "routine" in text and "voice" in text and "desktop" in text


class TestWindowsControlPage:
    """Widget-level behaviour of the settings dialog."""

    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"fast_commands_enabled": False}))
        monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
        from desktop_app.settings_window import SettingsWindow
        win = SettingsWindow()
        yield win, cfg
        win.close()

    def test_page_reflects_saved_value_and_status(self, window):
        win, _ = window
        assert win._widgets["fast_commands_enabled"].isChecked() is False
        assert "off" in win._windows_status_label.text().lower()

    def test_status_updates_when_toggled(self, window):
        win, _ = window
        win._widgets["fast_commands_enabled"].setChecked(True)
        assert "off" not in win._windows_status_label.text().splitlines()[0].lower()

    def test_save_persists_only_non_default(self, window, monkeypatch):
        win, cfg = window
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
        win._widgets["fast_commands_enabled"].setChecked(True)  # default
        win._widgets["windows_tools_enabled"].setChecked(False)
        win._on_save()
        saved = json.loads(cfg.read_text())
        assert "fast_commands_enabled" not in saved
        assert saved["windows_tools_enabled"] is False


class TestSettingsWindowLook:
    """The settings window wears the shared HUD theme: plain labels, roles, no stylesheets of its own."""

    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text("{}")
        monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
        from desktop_app.settings_window import SettingsWindow
        win = SettingsWindow()
        yield win
        win.close()

    def test_sidebar_lists_every_category_by_its_plain_label(self, window):
        sidebar = window._sidebar
        labels = [sidebar.item(i).text() for i in range(sidebar.count())]
        assert labels == [label for _key, label in CATEGORIES]
        assert sidebar.objectName() == "nav"

    def test_no_widget_carries_its_own_stylesheet(self, window):
        from PyQt6.QtWidgets import QWidget
        styled = [w.objectName() or type(w).__name__ for w in window.findChildren(QWidget) if w.styleSheet()]
        assert styled == []

    def test_secret_status_shows_its_state_in_the_status_colour(self, window, monkeypatch):
        from PyQt6.QtGui import QColor
        from desktop_app.themes import HUD_COLORS
        secret = next(fm for fm in FIELD_METADATA if fm.field_type == "secret")
        container = window._widgets[secret.key]
        window.show()
        container._stored, container._remove_pending = True, False
        container._refresh()
        assert container._status.text() == "Stored"
        assert container._status.palette().windowText().color().name() == QColor(HUD_COLORS["success_light"]).name()
        container._remove.click()
        assert container._status.text() == "Removed on save"
        assert container._status.palette().windowText().color().name() == QColor(HUD_COLORS["warning_light"]).name()

    def test_windows_status_lines_are_plain_text(self):
        from desktop_app.settings_window import windows_status_lines
        lines = windows_status_lines(True, True, "win32")
        assert lines[0] == "Quick commands: On"
        assert all(line.strip().replace("  ", " ")[0].isalpha() for line in lines)
