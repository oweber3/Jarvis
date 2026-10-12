"""Settings, Speaking page: choosing the voice engine and voice, and previewing it before saving."""
import json
import time
from types import SimpleNamespace

import numpy as np
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


@pytest.fixture
def previews(monkeypatch):
    """Replaces synthesis and playback; records the values each preview was asked for."""
    from desktop_app import voice_panel

    asked = []
    state = {"result": None}

    def render(values, text, progress=None):
        asked.append((dict(values), text))
        if state["result"] is not None:
            return state["result"]
        engine = values.get("tts_engine") or "piper"
        return SimpleNamespace(samples=np.ones(10, dtype=np.int16), sample_rate=22050, engine=engine, note=None)

    monkeypatch.setattr(voice_panel, "render_preview", render)
    monkeypatch.setattr(voice_panel, "play", lambda result: None)
    return asked, state


def _choose(combo, value):
    index = combo.findData(value)
    assert index >= 0, value
    combo.setCurrentIndex(index)


def _wait_for(qapp, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


@pytest.mark.unit
def test_the_engine_choice_offers_piper_kokoro_and_chatterbox_with_piper_by_default(window_with):
    from desktop_app.settings_window import FIELD_METADATA

    engine = next(fm for fm in FIELD_METADATA if fm.key == "tts_engine")
    assert [value for value, _ in engine.choices] == ["piper", "kokoro", "chatterbox"]
    hint = engine.description.lower()
    assert "lightest" in hint and "more natural" in hint and "graphics card" in hint and "memory" in hint
    win, _ = window_with({})
    assert win._widgets["tts_engine"].currentData() == "piper"


@pytest.mark.unit
def test_each_engine_shows_its_own_voice_and_speed(window_with):
    win, _ = window_with({})
    assert win.is_field_shown("tts_piper_voice") and not win.is_field_shown("tts_kokoro_voice")
    _choose(win._widgets["tts_engine"], "kokoro")
    assert win.is_field_shown("tts_kokoro_voice") and win.is_field_shown("tts_kokoro_speed")
    assert not win.is_field_shown("tts_piper_voice") and not win.is_field_shown("tts_piper_length_scale")


@pytest.mark.unit
def test_the_voice_pickers_list_every_offered_voice():
    from desktop_app.settings_window import FIELD_METADATA
    from jarvis.output.kokoro import KOKORO_VOICES
    from jarvis.output.piper_voices import piper_voices

    fields = {fm.key: fm for fm in FIELD_METADATA}
    assert [v for v, _ in fields["tts_piper_voice"].choices] == list(piper_voices())
    assert [v for v, _ in fields["tts_kokoro_voice"].choices] == list(KOKORO_VOICES)


@pytest.mark.unit
def test_voice_cloning_asks_for_your_own_voice_or_consent():
    from desktop_app.settings_window import FIELD_METADATA

    clone = next(fm for fm in FIELD_METADATA if fm.key == "tts_chatterbox_audio_prompt")
    assert "own voice" in clone.description and "consent" in clone.description


@pytest.mark.unit
def test_preview_speaks_the_unsaved_choice(window_with, previews, qapp):
    asked, _ = previews
    win, cfg = window_with({})
    _choose(win._widgets["tts_engine"], "kokoro")
    _choose(win._widgets["tts_kokoro_voice"], "bf_emma")
    win._widgets["tts_kokoro_speed"].setValue(1.3)
    win.voice_panel.preview_button.click()
    _wait_for(qapp, lambda: asked and win.voice_panel.preview_button.isEnabled())
    values, text = asked[-1]
    assert (values["tts_engine"], values["tts_kokoro_voice"]) == ("kokoro", "bf_emma")
    assert values["tts_kokoro_speed"] == pytest.approx(1.3)
    assert text.strip()
    assert "tts_engine" not in json.loads(cfg.read_text()), "previewing never saves"


@pytest.mark.unit
def test_preview_says_when_piper_stood_in_for_kokoro(window_with, previews, qapp):
    _asked, state = previews
    state["result"] = SimpleNamespace(samples=np.ones(10, dtype=np.int16), sample_rate=22050, engine="piper",
                                      note="the Kokoro voice files could not be downloaded")
    win, _ = window_with({"tts_engine": "kokoro"})
    win.voice_panel.preview_button.click()
    _wait_for(qapp, lambda: "could not be downloaded" in win.voice_panel.result_label.text())
    assert "Piper" in win.voice_panel.result_label.text()


@pytest.mark.unit
def test_the_panel_shows_download_size_and_graphics_card_as_information(window_with, monkeypatch):
    from jarvis.output import kokoro

    monkeypatch.setattr(kokoro, "kokoro_files_present", lambda *_: False)
    monkeypatch.setattr(kokoro, "gpu_acceleration_available", lambda: True)
    win, _ = window_with({"tts_engine": "kokoro"})
    win.voice_panel.refresh()
    status = win.voice_panel.status_label.text()
    assert f"{kokoro.KOKORO_DOWNLOAD_MB} MB" in status
    assert "graphics card" in status.lower()
    assert "download" in win.voice_panel.preview_button.text().lower()
    assert win._widgets["tts_engine"].currentData() == "kokoro", "hardware never changes the engine"


@pytest.mark.unit
def test_chatterbox_has_no_preview_button(window_with):
    win, _ = window_with({"tts_engine": "chatterbox"})
    win.voice_panel.refresh()
    assert not win.voice_panel.preview_button.isEnabled()


@pytest.mark.unit
def test_saving_a_kokoro_voice_writes_plain_keys(window_with):
    win, cfg = window_with({})
    _choose(win._widgets["tts_engine"], "kokoro")
    _choose(win._widgets["tts_kokoro_voice"], "af_heart")
    win._on_save()
    saved = json.loads(cfg.read_text())
    assert saved["tts_engine"] == "kokoro" and saved["tts_kokoro_voice"] == "af_heart"
    assert "tts_kokoro_speed" not in saved, "an unchanged default is not written"
