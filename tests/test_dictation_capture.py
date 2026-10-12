"""Capturing speech for the web chat's microphone button (dictation.spec.md, Capture for a caller).

The engine records and transcribes on request and hands the text back to its caller, which is the only
place the text goes: nothing is pasted, nothing is saved to the history and nothing is logged. Audio,
Whisper and beeps are faked, so no real microphone, clipboard or speaker is touched.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _dictation_deps():
    pytest.importorskip("pynput")
    pytest.importorskip("numpy")
    pytest.importorskip("sounddevice")


SECRET = "the quarterly figures are private"


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Ui:
    """Mirrors the daemon's dictation callbacks: face state and listener pause."""

    def __init__(self) -> None:
        self.state = "IDLE"
        self.listener_paused = False
        self.states = []

    def on_start(self) -> None:
        self.listener_paused = True
        self.state = "DICTATING"
        self.states.append(self.state)

    def on_processing(self) -> None:
        self.state = "DICTATION_PROCESSING"
        self.states.append(self.state)

    def on_end(self) -> None:
        self.listener_paused = False
        self.state = "IDLE"
        self.states.append(self.state)


def _engine(ui, tmp_path, *, model_ref=None, backend="faster-whisper", **extra):
    from jarvis.dictation import dictation_engine as de
    from jarvis.dictation.history import DictationHistory

    return de.DictationEngine(
        whisper_model_ref=model_ref or (lambda: MagicMock()),
        whisper_backend_ref=lambda: backend,
        mlx_repo_ref=lambda: None,
        hotkey="ctrl+shift+d",
        on_dictation_start=ui.on_start,
        on_dictation_processing_start=ui.on_processing,
        on_dictation_end=ui.on_end,
        history=DictationHistory(path=tmp_path / "history.json"),
        **extra,
    )


class _Streams:
    """A fake audio backend that remembers every stream it opened."""

    def __init__(self, sd) -> None:
        sd.query_devices.return_value = {"max_input_channels": 1, "default_samplerate": 16000}
        self.opened = []
        sd.InputStream.side_effect = self._open

    def _open(self, **kwargs):
        stream = MagicMock()
        stream.callback = kwargs.get("callback")
        self.opened.append(stream)
        return stream

    @property
    def released(self) -> bool:
        return bool(self.opened) and all(s.close.called for s in self.opened)


def _speak(engine, seconds: float = 1.0):
    import numpy as np

    engine._capture_callback_for_tests(np.ones((int(16000 * seconds), 1), dtype=np.float32) * 0.1)


@pytest.fixture
def world(tmp_path):
    from jarvis.dictation import dictation_engine as de

    ui = _Ui()
    engine = _engine(ui, tmp_path)
    pasted = []
    with patch.object(de, "sd") as sd, \
         patch.object(de, "_play_beep"), \
         patch.object(de, "_clipboard_paste", side_effect=pasted.append), \
         patch.object(engine, "_transcribe", return_value=SECRET) as transcribe:
        streams = _Streams(sd)
        engine._capture_callback_for_tests = lambda data: streams.opened[-1].callback(data, len(data), None, None)
        yield SimpleNamespace(de=de, ui=ui, engine=engine, pasted=pasted, streams=streams, sd=sd,
                              transcribe=transcribe, tmp_path=tmp_path)


def test_a_capture_returns_the_transcript_and_pastes_nothing(world):
    token = world.engine.start_capture(max_seconds=30)
    assert world.ui.listener_paused and world.ui.state == "DICTATING"
    _speak(world.engine)

    assert world.engine.stop_capture(token) == SECRET

    assert world.pasted == []
    assert world.engine.history.get_all() == []
    assert not world.ui.listener_paused and world.ui.state == "IDLE"
    assert world.ui.states == ["DICTATING", "DICTATION_PROCESSING", "IDLE"]
    assert world.streams.released


def test_cancelling_releases_the_microphone_without_transcribing(world):
    token = world.engine.start_capture(max_seconds=30)
    _speak(world.engine)

    assert world.engine.cancel_capture(token) is True

    world.transcribe.assert_not_called()
    assert world.streams.released
    assert not world.ui.listener_paused and world.ui.state == "IDLE"
    assert world.engine.stop_capture(token) is None  # nothing left to stop


def test_a_stale_or_unknown_capture_is_ignored(world):
    token = world.engine.start_capture(max_seconds=30)
    assert world.engine.stop_capture(token + 1) is None
    assert world.engine.cancel_capture(token + 1) is False
    assert world.engine.capture_state(token + 1) is None
    assert world.engine.capture_state(token) == "recording"
    assert world.ui.listener_paused  # the real capture is untouched
    world.engine.cancel_capture(token)


def test_a_second_capture_is_refused_while_one_is_open(world):
    token = world.engine.start_capture(max_seconds=30)
    with pytest.raises(world.de.CaptureRefused) as refused:
        world.engine.start_capture(max_seconds=30)
    assert refused.value.reason == "busy"
    assert len(world.streams.opened) == 1
    world.engine.cancel_capture(token)


def test_a_capture_is_refused_while_the_hotkey_is_dictating(world):
    world.engine._start_recording()
    assert _wait_until(lambda: world.engine._stream is not None)
    with pytest.raises(world.de.CaptureRefused) as refused:
        world.engine.start_capture(max_seconds=30)
    assert refused.value.reason == "busy"
    world.engine._stop_recording(discard=True)


def test_the_hotkey_does_not_start_a_second_recording_during_a_capture(world):
    from pynput import keyboard

    token = world.engine.start_capture(max_seconds=30)
    for key in (keyboard.Key.ctrl_l, keyboard.Key.shift_l, keyboard.KeyCode.from_char("d")):
        world.engine._on_key_press(key)
    time.sleep(0.1)

    assert not world.engine.is_recording
    assert len(world.streams.opened) == 1
    world.engine.cancel_capture(token)


def test_a_capture_is_refused_until_whisper_is_loaded(tmp_path):
    from jarvis.dictation import dictation_engine as de

    ui = _Ui()
    engine = _engine(ui, tmp_path, model_ref=lambda: None)
    with patch.object(de, "sd"), patch.object(de, "_play_beep"):
        with pytest.raises(de.CaptureRefused) as refused:
            engine.start_capture(max_seconds=30)
    assert refused.value.reason == "not_ready"
    assert ui.states == [] and not ui.listener_paused


def test_an_mlx_backend_needs_no_loaded_model(tmp_path):
    from jarvis.dictation import dictation_engine as de

    ui = _Ui()
    engine = _engine(ui, tmp_path, model_ref=lambda: None, backend="mlx")
    with patch.object(de, "sd") as sd, patch.object(de, "_play_beep"):
        streams = _Streams(sd)
        token = engine.start_capture(max_seconds=30)
        assert engine.cancel_capture(token) is True
        assert streams.released


def test_a_capture_is_refused_without_an_audio_backend(world):
    with patch.object(world.de, "sd", None):
        with pytest.raises(world.de.CaptureRefused) as refused:
            world.engine.start_capture(max_seconds=30)
    assert refused.value.reason == "unavailable"
    assert world.ui.states == []


def test_a_microphone_that_will_not_open_leaves_nothing_behind(world):
    world.sd.InputStream.side_effect = OSError("device busy")

    with pytest.raises(world.de.CaptureRefused) as refused:
        world.engine.start_capture(max_seconds=30)

    assert refused.value.reason == "microphone"
    assert not world.ui.listener_paused and world.ui.state == "IDLE"
    world.sd.InputStream.side_effect = world.streams._open
    token = world.engine.start_capture(max_seconds=30)  # the engine is free again
    world.engine.cancel_capture(token)


def test_a_microphone_that_will_not_start_is_released(world):
    def open_failing(**kwargs):
        stream = MagicMock()
        stream.start.side_effect = OSError("cannot start")
        world.streams.opened.append(stream)
        return stream

    world.sd.InputStream.side_effect = open_failing
    with pytest.raises(world.de.CaptureRefused) as refused:
        world.engine.start_capture(max_seconds=30)
    assert refused.value.reason == "microphone"
    assert world.streams.released
    assert not world.ui.listener_paused


def test_the_length_cap_releases_the_microphone_and_keeps_the_audio(world):
    token = world.engine.start_capture(max_seconds=0.2)
    _speak(world.engine)

    assert _wait_until(lambda: world.engine.capture_state(token) == "full")
    assert world.streams.released
    assert world.ui.listener_paused  # still the caller's to finish, or to cancel
    assert world.engine.stop_capture(token) == SECRET
    assert not world.ui.listener_paused


def test_audio_that_is_too_short_gives_an_empty_transcript(world):
    token = world.engine.start_capture(max_seconds=30)
    _speak(world.engine, seconds=0.1)

    assert world.engine.stop_capture(token) == ""
    world.transcribe.assert_not_called()
    assert not world.ui.listener_paused


def test_no_audio_at_all_gives_an_empty_transcript(world):
    token = world.engine.start_capture(max_seconds=30)
    assert world.engine.stop_capture(token) == ""
    assert not world.ui.listener_paused


def test_stopping_the_engine_releases_a_capture(world):
    world.engine.start_capture(max_seconds=30)

    world.engine.stop()

    assert world.streams.released
    assert not world.ui.listener_paused and world.ui.state == "IDLE"


def test_the_custom_dictionary_applies_to_a_capture(tmp_path):
    from jarvis.dictation import dictation_engine as de

    ui = _Ui()
    engine = _engine(ui, tmp_path, custom_dictionary=["jarvice -> Jarvis"])
    with patch.object(de, "sd") as sd, patch.object(de, "_play_beep"), \
         patch.object(engine, "_transcribe", return_value="hello jarvice"):
        streams = _Streams(sd)
        token = engine.start_capture(max_seconds=30)
        import numpy as np
        streams.opened[-1].callback(np.ones((16000, 1), dtype=np.float32) * 0.1, 16000, None, None)
        assert engine.stop_capture(token) == "hello Jarvis"


def test_the_transcript_is_never_logged_even_with_filler_removal(tmp_path):
    from jarvis.dictation import dictation_engine as de

    ui = _Ui()
    cleaned = "the quarterly figures are private, tidied"
    engine = _engine(ui, tmp_path, filler_removal=True, cfg=SimpleNamespace())
    logged = []
    backend = SimpleNamespace(direct=lambda *a, **k: cleaned)
    with patch.object(de, "sd") as sd, patch.object(de, "_play_beep"), \
         patch.object(de, "debug_log", side_effect=lambda message, *_a, **_k: logged.append(str(message))), \
         patch("jarvis.llm.get_llm_backend", return_value=backend), \
         patch("jarvis.llm.resolve_model", return_value="m"), \
         patch.object(engine, "_transcribe", return_value=SECRET):
        streams = _Streams(sd)
        token = engine.start_capture(max_seconds=30)
        import numpy as np
        streams.opened[-1].callback(np.ones((16000, 1), dtype=np.float32) * 0.1, 16000, None, None)
        assert engine.stop_capture(token) == cleaned

    assert logged, "key points are still logged"
    assert not [m for m in logged if "quarterly" in m or "tidied" in m]


def test_a_capture_can_follow_a_finished_one(world):
    first = world.engine.start_capture(max_seconds=30)
    _speak(world.engine)
    world.engine.stop_capture(first)

    second = world.engine.start_capture(max_seconds=30)
    assert second != first
    assert world.engine.capture_state(second) == "recording"
    world.engine.cancel_capture(second)


def test_the_blocker_says_why_a_capture_could_not_start_now(world):
    assert world.engine.capture_blocker() is None
    token = world.engine.start_capture(max_seconds=30)
    assert world.engine.capture_blocker() == "busy"
    world.engine.cancel_capture(token)
    assert world.engine.capture_blocker() is None
    with patch.object(world.de, "sd", None):
        assert world.engine.capture_blocker() == "unavailable"


def test_the_blocker_reports_whisper_not_loaded(tmp_path):
    from jarvis.dictation import dictation_engine as de

    engine = _engine(_Ui(), tmp_path, model_ref=lambda: None)
    with patch.object(de, "sd"):
        assert engine.capture_blocker() == "not_ready"
