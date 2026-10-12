"""Repro tests for defects found in dictation, daemon wiring and the desktop app.

Each test describes the user-visible behaviour the spec promises and fails
on the current code. Nothing here opens a real microphone, keyboard hook,
clipboard, network or model: audio, paste and Whisper are faked.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Dictation: a finished dictation must not end the one that is recording now
# ---------------------------------------------------------------------------


class _DaemonLikeUi:
    """Mirrors the daemon's dictation callbacks: face state + listener pause."""

    def __init__(self) -> None:
        self.state = "IDLE"
        self.listener_paused = False
        self.started = threading.Event()

    def on_start(self) -> None:
        self.listener_paused = True
        self.state = "DICTATING"
        self.started.set()

    def on_processing(self) -> None:
        self.state = "DICTATION_PROCESSING"

    def on_end(self) -> None:
        self.listener_paused = False
        self.state = "IDLE"


@pytest.fixture
def dictation_deps():
    pytest.importorskip("pynput")
    pytest.importorskip("numpy")
    pytest.importorskip("sounddevice")


def test_previous_dictation_finishing_does_not_end_the_next_recording(dictation_deps, tmp_path):
    """Hold, release, and hold again while Whisper is still transcribing the
    first sentence: the second recording must keep the listener paused and
    the face in DICTATING until it is itself released (dictation.spec.md,
    Core Flow and "Pause flag on the main listener")."""
    import numpy as np
    from jarvis.dictation import dictation_engine as de
    from jarvis.dictation.history import DictationHistory

    ui = _DaemonLikeUi()
    transcribing = threading.Event()
    release_whisper = threading.Event()
    first_done = threading.Event()

    engine = de.DictationEngine(
        whisper_model_ref=lambda: MagicMock(),
        whisper_backend_ref=lambda: "faster-whisper",
        mlx_repo_ref=lambda: None,
        hotkey="ctrl+shift+d",
        on_dictation_start=ui.on_start,
        on_dictation_processing_start=ui.on_processing,
        on_dictation_end=ui.on_end,
        history=DictationHistory(path=tmp_path / "history.json"),
    )

    def slow_transcribe(_audio):
        transcribing.set()
        release_whisper.wait(5.0)
        return "first sentence"

    with patch.object(de, "sd") as fake_sd, \
         patch.object(de, "_play_beep"), \
         patch.object(de, "_clipboard_paste", side_effect=lambda _t: first_done.set()), \
         patch.object(engine, "_transcribe", side_effect=slow_transcribe):
        fake_sd.query_devices.return_value = {"max_input_channels": 1, "default_samplerate": 16000}
        fake_sd.InputStream.return_value = MagicMock()

        # First dictation: one second of audio, then the hotkey is released.
        engine._start_recording()
        assert ui.started.wait(5.0)
        assert _wait_until(lambda: engine._stream is not None)
        engine._audio_callback(np.zeros((16000, 1), dtype=np.float32), 16000, None, None)
        engine._stop_recording()
        assert transcribing.wait(5.0), "first dictation never reached Whisper"

        # Second dictation starts while the first is still transcribing.
        ui.started.clear()
        engine._start_recording()
        assert ui.started.wait(5.0)
        assert engine.is_recording

        # The first dictation now finishes and pastes.
        release_whisper.set()
        assert first_done.wait(5.0)
        _wait_until(lambda: ui.state == "IDLE", timeout=1.0)

        try:
            assert engine.is_recording, "second recording should still be running"
            assert ui.listener_paused and ui.state == "DICTATING", (
                f"second recording is live but the UI says state={ui.state}, "
                f"listener_paused={ui.listener_paused}"
            )
        finally:
            engine._stop_recording(discard=True)


# ---------------------------------------------------------------------------
# Dictation: a hands-free start that fails must not leave hands-free armed
# ---------------------------------------------------------------------------


def _hotkey_keys():
    from pynput import keyboard

    return keyboard.Key.ctrl_l, keyboard.Key.shift_l, keyboard.KeyCode.from_char("d")


def test_failed_hands_free_start_does_not_turn_the_next_hold_into_hands_free(dictation_deps, tmp_path):
    """Double-tap while the microphone cannot be opened (device busy or
    unplugged): the second tap arms hands-free, then the stream fails and the
    session is abandoned. The next ordinary hold must stop when the hotkey is
    released (dictation.spec.md, Hold-to-Dictate step 3), not keep recording
    as if hands-free were on."""
    from jarvis.dictation import dictation_engine as de
    from jarvis.dictation.history import DictationHistory

    ctrl, shift, d = _hotkey_keys()
    started = threading.Event()
    ended = threading.Event()
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

    engine = de.DictationEngine(
        whisper_model_ref=lambda: MagicMock(),
        whisper_backend_ref=lambda: "faster-whisper",
        mlx_repo_ref=lambda: None,
        hotkey="ctrl+shift+d",
        on_dictation_start=started.set,
        on_dictation_end=ended.set,
        history=DictationHistory(path=tmp_path / "history.json"),
    )

    with patch.object(de, "sd") as fake_sd, \
         patch.object(de, "_play_beep"), \
         patch.object(de, "_clipboard_paste"), \
         patch.object(de, "open_input_stream", side_effect=fake_open), \
         patch.object(engine, "_transcribe", return_value=""):
        fake_sd.query_devices.return_value = {"max_input_channels": 1, "default_samplerate": 16000}

        engine._on_key_press(ctrl)
        engine._on_key_press(shift)

        # Tap 1: quick press and release.
        engine._on_key_press(d)
        assert started.wait(5.0)
        engine._on_key_release(d)
        assert ended.wait(5.0)

        # Tap 2: the microphone is slow to fail; the quick release arms hands-free.
        started.clear()
        ended.clear()
        engine._on_key_press(d)
        assert opening.wait(5.0)
        engine._on_key_release(d)
        let_open_fail.set()
        assert ended.wait(5.0), "abandoned session should end"
        assert not engine.is_recording

        # Later, an ordinary hold: press, speak, release.
        opens["n"] = 10
        started.clear()
        engine._on_key_press(d)
        assert started.wait(5.0)
        assert _wait_until(lambda: engine._stream is not None)
        engine._record_start_time -= 2.0  # held for two seconds
        engine._on_key_release(d)

        try:
            assert not engine.is_recording, (
                "releasing the hotkey after a normal hold left dictation recording "
                f"(hands_free={engine._hands_free})"
            )
        finally:
            engine._stop_recording(discard=True)


# ---------------------------------------------------------------------------
# Dictation: holding the hotkey before Whisper has loaded gives no feedback
# ---------------------------------------------------------------------------


def test_hotkey_before_whisper_loads_plays_not_ready_beep(dictation_deps, tmp_path):
    """dictation.spec.md, Edge Cases: "Whisper not yet loaded: Play 'not ready'
    beep, skip". Pressing the hotkey during start-up must give that cue rather
    than silently doing nothing."""
    from jarvis.dictation import dictation_engine as de
    from jarvis.dictation.history import DictationHistory

    engine = de.DictationEngine(
        whisper_model_ref=lambda: None,
        whisper_backend_ref=lambda: "faster-whisper",
        mlx_repo_ref=lambda: None,
        hotkey="ctrl+shift+d",
        history=DictationHistory(path=tmp_path / "history.json"),
    )
    with patch.object(de, "sd"), patch.object(de, "_play_beep") as beep:
        engine._start_recording()
        assert _wait_until(lambda: not engine.is_recording)
        _wait_until(lambda: beep.called, timeout=0.5)
        assert beep.called, "no 'not ready' beep when Whisper is still loading"


# ---------------------------------------------------------------------------
# Desktop: Settings + "Restart Jarvis now" must make the tray's Chat follow
# the saved web chat switch
# ---------------------------------------------------------------------------


def test_tray_chat_follows_web_chat_switch_after_settings_restart(qapp, monkeypatch):
    """Settings says "Restart Jarvis after changing it" and the tray then offers
    "Restart Jarvis now to apply changes?". After saying yes, the daemon
    serves the web chat, so the tray's Chat entry must open it
    (webchat.spec.md: the tray's Chat opens the web chat while
    web_chat_enabled is on)."""
    import desktop_app.app as app_mod
    import desktop_app.settings_window as settings_mod
    from PyQt6.QtWidgets import QDialog, QMessageBox

    class _SavedSettings:
        changed_values = {"web_chat_enabled": True}

        def exec(self):
            return QDialog.DialogCode.Accepted

    tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
    tray.is_listening = True
    tray.is_bundled = False
    tray._web_chat_port = None  # web chat was off when the app launched
    tray.chat_window = None
    tray.web_chat_window = None
    tray._chat_submit_fn = None
    tray._chat_cancel_fn = None
    tray._chat_control_fn = None
    tray._reply_mode = "local"
    tray._apply_settings_now = MagicMock()
    tray.stop_daemon = MagicMock()
    tray.start_daemon = MagicMock()
    tray.show_web_chat = MagicMock()

    monkeypatch.setattr(settings_mod, "SettingsWindow", _SavedSettings)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    # The saved config now has the web chat on.
    monkeypatch.setattr(app_mod, "web_chat_port_if_enabled", lambda: 8766)
    # Never build the Qt chat window in this test.
    monkeypatch.setattr("desktop_app.chat_window.ChatWindow", MagicMock())

    tray.show_settings()
    assert tray.start_daemon.called, "the user accepted the restart"

    tray.show_chat()
    tray.show_web_chat.assert_called_once_with(8766)


# ---------------------------------------------------------------------------
# Desktop: the 2 s status poll firing inside stop_daemon's wait loop
# ---------------------------------------------------------------------------


def test_user_stop_is_not_reported_as_a_crash_when_the_status_poll_fires(qapp, monkeypatch):
    """stop_daemon pumps Qt events while it waits for the daemon to exit, so
    the 2-second check_daemon_status timer runs inside that loop. When it
    fires just after the process exits, a Stop the user asked for must still
    read as a normal stop: no "ended unexpectedly" warning, no "crashed"
    chat state, no "Failed to stop" error."""
    import desktop_app.app as app_mod

    tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
    tray.is_bundled = False
    tray.is_listening = True
    tray.daemon_thread = None
    tray.log_viewer = MagicMock()
    tray.face_window = MagicMock()
    tray.log_signals = MagicMock()
    tray.tray_icon = MagicMock()
    tray.toggle_action = MagicMock()
    tray.status_action = MagicMock()
    tray.update_icon = MagicMock()
    tray._set_chat_daemon_status = MagicMock()
    tray._show_configured_reply_mode = MagicMock()
    tray._set_face_asleep = MagicMock()
    tray._daemon_stop_expected = False
    tray._chat_submit_fn = lambda text: None

    exited = {"value": False}
    proc = MagicMock()
    proc.poll.side_effect = lambda: 0 if exited["value"] else None
    proc.stdin = MagicMock()
    tray.daemon_process = proc

    pumps = {"n": 0}

    def process_events():
        pumps["n"] += 1
        if pumps["n"] == 3:
            # The daemon finishes its shutdown, and the status timer is due.
            exited["value"] = True
            tray.check_daemon_status()

    tray.app = MagicMock()
    tray.app.processEvents.side_effect = process_events
    monkeypatch.setattr(app_mod, "DiaryUpdateDialog", MagicMock())
    monkeypatch.setattr(app_mod.time, "sleep", lambda _s: None)

    tray.stop_daemon(show_diary_dialog=True)

    toasts = [c.args[1] for c in tray.tray_icon.showMessage.call_args_list]
    logs = "".join(str(c.args[0]) for c in tray.log_signals.new_log.emit.call_args_list)
    statuses = [c.args[0] for c in tray._set_chat_daemon_status.call_args_list]
    assert "Voice assistant process ended unexpectedly" not in toasts, toasts
    assert "crashed" not in statuses, statuses
    assert "Failed to stop" not in logs, logs
