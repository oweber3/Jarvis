"""Settings window layout: few pages, advanced settings behind a switch, rows that follow the choices they
depend on, and the cloud modes side by side."""
import ast
import json
from collections import Counter
from pathlib import Path

import pytest


@pytest.fixture
def window_with(qapp, tmp_path, monkeypatch):
    opened = []

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

    yield open_with
    for win in opened:
        win.close()


def _choose(combo, value):
    index = combo.findData(value)
    assert index >= 0, value
    combo.setCurrentIndex(index)


@pytest.mark.unit
def test_advanced_page_and_fields_appear_only_when_asked_for(window_with):
    from desktop_app.settings_window import ADVANCED_CATEGORY, CATEGORIES, FIELD_METADATA

    advanced_label = dict(CATEGORIES)[ADVANCED_CATEGORY]
    advanced = [fm.key for fm in FIELD_METADATA if fm.advanced]
    assert advanced
    win, _cfg = window_with({})
    assert advanced_label not in win.visible_pages()
    assert not any(win.is_field_shown(key) for key in advanced)

    win.set_show_advanced(True)
    assert advanced_label in win.visible_pages()
    assert win.is_field_shown("echo_tolerance")


@pytest.mark.unit
def test_every_field_on_the_advanced_page_is_advanced():
    from desktop_app.settings_window import ADVANCED_CATEGORY, FIELD_METADATA

    on_page = [fm for fm in FIELD_METADATA if fm.category == ADVANCED_CATEGORY]
    assert on_page and all(fm.advanced for fm in on_page)


@pytest.mark.unit
def test_show_advanced_is_remembered_after_save(window_with):
    from desktop_app.settings_window import SHOW_ADVANCED_KEY

    win, cfg = window_with({})
    win.set_show_advanced(True)
    win._on_save()
    assert json.loads(cfg.read_text())[SHOW_ADVANCED_KEY] is True

    reopened, _ = window_with(json.loads(cfg.read_text()))
    assert reopened.is_field_shown("echo_tolerance")
    reopened.set_show_advanced(False)
    reopened._on_save()
    assert SHOW_ADVANCED_KEY not in json.loads(cfg.read_text())


@pytest.mark.unit
def test_dictation_thinking_shows_only_with_filler_removal(window_with):
    win, _cfg = window_with({"dictation_enabled": True, "dictation_filler_removal": False})
    assert not win.is_field_shown("dictation_thinking_enabled")
    win._widgets["dictation_filler_removal"].setChecked(True)
    assert win.is_field_shown("dictation_thinking_enabled")
    win._widgets["dictation_enabled"].setChecked(False)
    assert not win.is_field_shown("dictation_filler_removal")
    assert not win.is_field_shown("dictation_thinking_enabled")


@pytest.mark.unit
def test_provider_choice_shows_that_providers_fields(window_with):
    win, _cfg = window_with({"llm_provider": "ollama"})
    assert win.is_field_shown("ollama_chat_model")
    assert not win.is_field_shown("llm_chat_model")
    assert not win.is_field_shown("llm_base_url")

    _choose(win._widgets["llm_provider"], "openai_compatible")
    assert win.is_field_shown("llm_chat_model")
    assert win.is_field_shown("llm_base_url")
    assert win.is_field_shown("embedding_model")
    assert not win.is_field_shown("ollama_chat_model")


@pytest.mark.unit
def test_ollama_chat_with_remote_embeddings_shows_both_connections(window_with):
    win, _cfg = window_with({"llm_provider": "ollama", "embedding_provider": "openai_compatible"})
    win.set_show_advanced(True)
    assert win.is_field_shown("ollama_chat_model")
    assert win.is_field_shown("ollama_base_url")
    assert win.is_field_shown("embedding_model")
    assert win.is_field_shown("embedding_base_url")


@pytest.mark.unit
def test_voice_engine_choice_shows_that_engines_options(window_with):
    win, _cfg = window_with({"tts_engine": "piper"})
    assert win.is_field_shown("tts_piper_length_scale")
    assert not win.is_field_shown("tts_chatterbox_audio_prompt")
    _choose(win._widgets["tts_engine"], "chatterbox")
    assert win.is_field_shown("tts_chatterbox_audio_prompt")
    assert not win.is_field_shown("tts_piper_length_scale")


@pytest.mark.unit
@pytest.mark.parametrize("values", [
    {"llm_provider": "ollama", "tts_engine": "piper"},
    {"llm_provider": "openai_compatible", "embedding_provider": "ollama", "tts_engine": "chatterbox"},
    {"llm_provider": "ollama", "tts_engine": "kokoro"},
])
def test_no_page_shows_two_rows_with_the_same_label(window_with, values):
    win, _cfg = window_with(values)
    win.set_show_advanced(True)
    shown = [(fm.category, fm.column, fm.label) for fm in win._fields if win.is_field_shown(fm.key)]
    repeated = [row for row, count in Counter(shown).items() if count > 1]
    assert not repeated


@pytest.mark.unit
def test_hidden_rows_keep_their_saved_values(window_with):
    win, cfg = window_with({"llm_provider": "ollama", "llm_chat_model": "remote-model"})
    assert not win.is_field_shown("llm_chat_model")
    win._widgets["low_power_mode"].setChecked(True)
    win._on_save()
    saved = json.loads(cfg.read_text())
    assert saved["llm_chat_model"] == "remote-model"
    assert saved["low_power_mode"] is True


@pytest.mark.unit
def test_codex_and_claude_settings_sit_side_by_side(window_with):
    from desktop_app.settings_window import FIELD_METADATA

    cloud = [fm for fm in FIELD_METADATA if fm.column]
    rows = {}
    for fm in cloud:
        rows.setdefault((fm.section, fm.label), set()).add(fm.column)
    assert rows and all(columns == {"Codex", "Claude"} for columns in rows.values())

    win, _cfg = window_with({})
    for fm in cloud:
        partner = next(o for o in cloud if (o.section, o.label) == (fm.section, fm.label) and o is not fm)
        assert win.grid_row(fm.key) == win.grid_row(partner.key)


@pytest.mark.unit
def test_every_field_belongs_to_a_named_section():
    from desktop_app.settings_window import FIELD_METADATA

    assert all(fm.section.strip() for fm in FIELD_METADATA)


@pytest.mark.unit
def test_planner_and_tool_selection_are_offered_as_advanced_settings():
    from desktop_app.settings_window import FIELD_METADATA

    fields = {fm.key: fm for fm in FIELD_METADATA}
    assert fields["planner_enabled"].advanced and fields["planner_enabled"].field_type == "bool"
    strategy = fields["tool_selection_strategy"]
    assert strategy.advanced
    assert {value for value, _ in strategy.choices} == {"all", "keyword", "embedding", "llm"}


@pytest.mark.unit
def test_config_loader_fallbacks_agree_with_the_defaults():
    """A value missing from config.json falls back to the defaults list; a different literal in the loader
    would be a second, silently diverging default."""
    import jarvis.config as config

    defaults = config.get_default_config()
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    loader = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "load_settings")
    disagreeing = []
    for node in ast.walk(loader):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "merged"
                and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)):
            key = node.args[0].value
            try:
                fallback = ast.literal_eval(node.args[1])
            except ValueError:
                continue
            if key in defaults and fallback != defaults[key]:
                disagreeing.append(key)
    assert not disagreeing


@pytest.mark.unit
def test_numbers_show_as_many_decimals_as_their_step(window_with):
    win, _cfg = window_with({"llm_chat_timeout_sec": 45.0, "gesture_snap_speed_response": 0.005})
    assert win._widgets["llm_chat_timeout_sec"].text() == "45 s"
    assert win._widgets["voice_wake_wait_seconds"].text() == "4.5 s"
    assert win._widgets["gesture_snap_speed_response"].text() == "0.005"
