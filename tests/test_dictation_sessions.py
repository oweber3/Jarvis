"""Dictation sessions that overlap: notices, hands-free and the "not ready" cue.

These follow the user through hold, release and press again while an
earlier dictation is still transcribing (dictation.spec.md, Core Flow and
Edge Cases). Audio, Whisper, beeps and paste are faked: nothing opens a real
microphone, keyboard hook, clipboard or speaker.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _dictation_deps():
    pytest.importorskip("pynput")
    pytest.importorskip("numpy")
    pytest.importorskip("sounddevice")


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _DaemonLikeUi:
    """Mirrors the daemon's dictation callbacks: face state and listener pause."""

    def __init__(self) -> None:
        self.state = "IDLE"
        self.listener_paused = False
        self.started = threading.Event()
        self.ended = threading.Event()

    def on_start(self) -> None:
        self.listener_paused = True
        self.state = "DICTATING"
        self.started.set()

    def on_processing(self) -> None:
        self.state = "DICTATION_PROCESSING"

    def on_end(self) -> None:
        self.listener_paused = False
        self.state = "IDLE"
        self.ended.set()


def _engine(ui, tmp_path, *, model_ref=None):
    from jarvis.dictation import dictation_engine as de
    from jarvis.dictation.history import DictationHistory

    return de.DictationEngine(
        whisper_model_ref=model_ref or (lambda: MagicMock()),
        whisper_backend_ref=lambda: "faster-whisper",
        mlx_repo_ref=lambda: None,
        hotkey="ctrl+shift+d",
        on_dictation_start=ui.on_start,
        on_dictation_processing_start=ui.on_processing,
        on_dictation_end=ui.on_end,
        history=DictationHistory(path=tmp_path / "history.json"),
    )


def _fake_audio(fake_sd):
    fake_sd.query_devices.return_value = {"max_input_channels": 1, "default_samplerate": 16000}
    fake_sd.InputStream.side_effect = lambda **_kw: MagicMock()


def _record_one_second_and_release(engine, ui):
    import numpy as np

    ui.started.clear()
    engine._start_recording()
    assert ui.started.wait(5.0)
    assert _wait_until(lambda: engine._stream is not None)
    engine._audio_callback(np.zeros((16000, 1), dtype=np.float32), 16000, None, None)
    engine._stop_recording()


def test_previous_dictation_finishing_does_not_end_the_next_recording(tmp_path):
    """Hold, release, and hold again while Whisper is still transcribing the
    first sentence: the second recording keeps the listener paused and the
    face in DICTATING until it is itself released, and only then do they
    return to idle."""
    import numpy as np
    from jarvis.dictation import dictation_engine as de

    ui = _DaemonLikeUi()
    engine = _engine(ui, tmp_path)
    first_in_whisper = threading.Event()
    release_first = threading.Event()
    pasted = []

    def transcribe(_audio):
        if not pasted:
            first_in_whisper.set()
            release_first.wait(5.0)
            return "first sentence"
        return "second sentence"

    with patch.object(de, "sd") as fake_sd, \
         patch.object(de, "_play_beep"), \
         patch.object(de, "_clipboard_paste", side_effect=pasted.append), \
         patch.object(engine, "_transcribe", side_effect=transcribe):
        _fake_audio(fake_sd)

        _record_one_second_and_release(engine, ui)
        assert first_in_whisper.wait(5.0)

        ui.started.clear()
        engine._start_recording()
        assert ui.started.wait(5.0)

        ui.ended.clear()
        release_first.set()
        assert _wait_until(lambda: pasted == ["first sentence"])
        time.sleep(0.1)  # give a wrong end notice the chance to land

        assert engine.is_recording
        assert ui.listener_paused and ui.state == "DICTATING", (ui.state, ui.listener_paused)
        assert not ui.ended.is_set()

        # Releasing the second hold ends the cycle as usual.
        assert _wait_until(lambda: engine._stream is not None)
        engine._audio_callback(np.zeros((16000, 1), dtype=np.float32), 16000, None, None)
        engine._stop_recording()
        assert ui.ended.wait(5.0)
        assert pasted == ["first sentence", "second sentence"]
        assert not ui.listener_paused and ui.state == "IDLE"


def test_newer_press_abandoned_silently_still_resumes_the_listener(tmp_path):
    """The first dictation finishes while a newer press is still checking for
    Whisper; that press then gives up without any notices of its own (Whisper
    unavailable). The end notice held back for it must still fire, so the
    listener is not left paused for good."""
    from jarvis.dictation import dictation_engine as de

    ui = _DaemonLikeUi()
    newer_checking = threading.Event()
    let_newer_fail = threading.Event()
    calls = {"n": 0}

    def model_ref():
        calls["n"] += 1
        if calls["n"] == 2:  # the newer press asks for Whisper
            newer_checking.set()
            let_newer_fail.wait(5.0)
            return None
        return MagicMock()

    engine = _engine(ui, tmp_path, model_ref=model_ref)
    release_first = threading.Event()
    first_done = threading.Event()

    with patch.object(de, "sd") as fake_sd, \
         patch.object(de, "_play_beep"), \
         patch.object(de, "_clipboard_paste", side_effect=lambda _t: first_done.set()), \
         patch.object(engine, "_transcribe", side_effect=lambda _a: release_first.wait(5.0) and "text"):
        _fake_audio(fake_sd)

        _record_one_second_and_release(engine, ui)

        engine._start_recording()  # newer press
        assert newer_checking.wait(5.0)

        ui.ended.clear()
        release_first.set()
        assert first_done.wait(5.0)
        time.sleep(0.1)
        assert ui.listener_paused, "end notice is held while the newer press is open"

        let_newer_fail.set()
        assert ui.ended.wait(5.0), "the held end notice never fired"
        assert not engine.is_recording
        assert not ui.listener_paused and ui.state == "IDLE"


def _hotkey_keys():
    from pynput import keyboard

    return keyboard.Key.ctrl_l, keyboard.Key.shift_l, keyboard.KeyCode.from_char("d")


def test_failed_hands_free_start_does_not_turn_the_next_hold_into_hands_free(tmp_path):
    """Double-tap while the microphone cannot be opened (device busy or
    unplugged): the second tap arms hands-free, then the stream fails and the
    session is abandoned. The next ordinary hold stops when the hotkey is
    released (dictation.spec.md, Hold-to-Dictate step 3)."""
    from jarvis.dictation import dictation_engine as de

    ctrl, shift, d = _hotkey_keys()
    ui = _DaemonLikeUi()
    opening = threading.Event()
    let_open_fail = threading.Event()
    opens = {"n": 0}

    def fake_open(_sd, rate, _ms, _kwargs, **_kw):
        opens["n"] += 1
        if opens["n"] == 2:  # the second tap of the double-tap
            opening.set()
            let_open_fail.wait(5.0)
            raise RuntimeError("Device unavailable", -9985)
        return MagicMock(), rate, 1

    engine = _engine(ui, tmp_path)

    with patch.object(de, "sd") as fake_sd, \
         patch.object(de, "_play_beep"), \
         patch.object(de, "_clipboard_paste"), \
         patch.object(de, "open_input_stream", side_effect=fake_open), \
         patch.object(engine, "_transcribe", return_value=""):
        _fake_audio(fake_sd)

        engine._on_key_press(ctrl)
        engine._on_key_press(shift)

        # Tap 1: quick press and release.
        engine._on_key_press(d)
        assert ui.started.wait(5.0)
        engine._on_key_release(d)
        assert ui.ended.wait(5.0)

        # Tap 2: the microphone is slow to fail; the quick release arms hands-free.
        ui.started.clear()
        ui.ended.clear()
        engine._on_key_press(d)
        assert opening.wait(5.0)
        engine._on_key_release(d)
        let_open_fail.set()
        assert ui.ended.wait(5.0), "abandoned session should end"
        assert not engine.is_recording

        # Later, an ordinary hold: press, speak for two seconds, release.
        opens["n"] = 10
        ui.started.clear()
        engine._on_key_press(d)
        assert ui.started.wait(5.0)
        assert _wait_until(lambda: engine._stream is not None)
        engine._record_start_time -= 2.0
        engine._on_key_release(d)

        try:
            assert not engine.is_recording, "releasing the hotkey after a normal hold left dictation recording"
        finally:
            engine._stop_recording(discard=True)


def test_hotkey_before_whisper_loads_plays_a_distinct_not_ready_beep_off_the_hook_thread(tmp_path):
    """dictation.spec.md, Edge Cases: "Whisper not yet loaded: Play 'not ready'
    beep, skip". The cue differs from the start and stop beeps and plays on
    a worker, never on the keyboard hook thread that pressed the hotkey."""
    from jarvis.dictation import dictation_engine as de

    ui = _DaemonLikeUi()
    engine = _engine(ui, tmp_path, model_ref=lambda: None)
    played = []

    def beep(wav):
        played.append((wav, threading.get_ident()))

    hook_thread = threading.get_ident()
    with patch.object(de, "sd"), patch.object(de, "_play_beep", side_effect=beep):
        engine._start_recording()
        assert _wait_until(lambda: not engine.is_recording)
        assert _wait_until(lambda: played)

    wav, thread_id = played[0]
    assert wav not in (de._get_start_beep(), de._get_stop_beep())
    assert thread_id != hook_thread
    assert not ui.started.is_set(), "nothing is recorded while Whisper is loading"
