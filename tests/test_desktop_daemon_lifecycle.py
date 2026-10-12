"""Tray behaviour across daemon starts and stops.

The tray follows the saved web chat switch from each daemon start, and a
Stop the user asked for always reads as a stop, even when the 2-second
status poll runs inside stop_daemon's wait loop. Processes, threads and
dialogs are fakes; nothing real is launched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _bare_tray(app_mod):
    tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
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
    tray.chat_window = None
    tray.web_chat_window = None
    tray._chat_submit_fn = None
    tray._chat_cancel_fn = None
    tray._chat_control_fn = None
    tray._reply_mode = "local"
    return tray


# ---------------------------------------------------------------------------
# Web chat switch
# ---------------------------------------------------------------------------


@pytest.fixture
def startable_tray(qapp, monkeypatch):
    """A subprocess-mode tray whose daemon start launches a fake process."""
    import desktop_app.app as app_mod

    tray = _bare_tray(app_mod)
    tray.is_bundled = False
    tray.is_listening = False
    tray.daemon_process = None
    tray.daemon_thread = None
    tray.log_reader_threads = []
    tray.app = MagicMock()
    tray.show_web_chat = MagicMock()
    monkeypatch.setattr(app_mod.subprocess, "Popen", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app_mod.threading, "Thread", MagicMock())
    monkeypatch.setattr("desktop_app.chat_window.ChatWindow", MagicMock())
    return app_mod, tray


def test_tray_chat_opens_the_web_chat_once_a_restart_picks_up_the_switch(startable_tray, monkeypatch):
    """webchat.spec.md: the tray's Chat opens the web chat while
    web_chat_enabled is on. Turning it on in Settings and restarting the
    daemon (the tray's "Restart Jarvis now") is enough."""
    app_mod, tray = startable_tray
    tray._web_chat_port = None  # off when the app launched
    monkeypatch.setattr(app_mod, "web_chat_port_if_enabled", lambda: 8766)

    tray.start_daemon()
    tray.show_chat()

    tray.show_web_chat.assert_called_once_with(8766)


def test_turning_the_web_chat_off_returns_the_tray_to_the_classic_chat(startable_tray, monkeypatch):
    app_mod, tray = startable_tray
    tray._web_chat_port = 8766
    old_window = MagicMock()
    tray.web_chat_window = old_window
    monkeypatch.setattr(app_mod, "web_chat_port_if_enabled", lambda: None)

    tray.start_daemon()
    tray.show_chat()

    tray.show_web_chat.assert_not_called()
    assert tray.chat_window is not None, "the classic chat opens"
    old_window.close.assert_called_once()
    assert tray.web_chat_window is None


def test_a_new_web_chat_port_is_used_after_restart(startable_tray, monkeypatch):
    app_mod, tray = startable_tray
    tray._web_chat_port = 8766
    tray.web_chat_window = MagicMock()
    monkeypatch.setattr(app_mod, "web_chat_port_if_enabled", lambda: 9001)

    tray.start_daemon()
    tray.show_chat()

    tray.show_web_chat.assert_called_once_with(9001)
    assert tray.web_chat_window is None, "the window on the old port is dropped"


# ---------------------------------------------------------------------------
# A user Stop with the status poll firing inside the wait loop
# ---------------------------------------------------------------------------


def _outcome(tray):
    toasts = [c.args[1] for c in tray.tray_icon.showMessage.call_args_list]
    logs = "".join(str(c.args[0]) for c in tray.log_signals.new_log.emit.call_args_list)
    statuses = [c.args[0] for c in tray._set_chat_daemon_status.call_args_list]
    return toasts, logs, statuses


def _assert_reads_as_a_stop(tray):
    toasts, logs, statuses = _outcome(tray)
    assert "Voice assistant process ended unexpectedly" not in toasts, toasts
    assert "Voice assistant is no longer listening" in toasts, toasts
    assert "crashed" not in statuses, statuses
    assert "Failed to stop" not in logs, logs
    assert tray.is_listening is False


def test_subprocess_stop_is_not_reported_as_a_crash_when_the_status_poll_fires(qapp, monkeypatch):
    import desktop_app.app as app_mod

    tray = _bare_tray(app_mod)
    tray.is_bundled = False
    tray.is_listening = True
    tray.daemon_thread = None
    tray._chat_submit_fn = lambda text: None

    exited = {"value": False}
    proc = MagicMock()
    proc.poll.side_effect = lambda: 0 if exited["value"] else None
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

    _assert_reads_as_a_stop(tray)
    assert tray.daemon_process is None


def test_bundled_stop_is_not_reported_as_a_crash_when_the_thread_ends_mid_pump(qapp, monkeypatch):
    """Bundled mode: the daemon thread finishes between the loop's check and
    the next event pump, which then delivers both the queued finished slot
    and the status poll."""
    import desktop_app.app as app_mod

    tray = _bare_tray(app_mod)
    tray.is_bundled = True
    tray.is_listening = True
    tray.daemon_process = None

    finished = {"value": False}
    thread = MagicMock()
    thread.isFinished.side_effect = lambda: finished["value"]
    tray.daemon_thread = thread

    pumps = {"n": 0}

    def process_events():
        pumps["n"] += 1
        if pumps["n"] == 3:
            finished["value"] = True
            tray.check_daemon_status()   # the status timer is due
            tray._on_daemon_finished()   # the queued finished signal

    tray.app = MagicMock()
    tray.app.processEvents.side_effect = process_events
    monkeypatch.setattr(app_mod, "DiaryUpdateDialog", MagicMock())
    monkeypatch.setattr("jarvis.daemon.set_diary_update_callbacks", MagicMock())
    monkeypatch.setattr("jarvis.daemon.request_stop", MagicMock())
    monkeypatch.setattr("jarvis.tools.confirmation.set_dialog_callback", MagicMock())
    monkeypatch.setattr(app_mod.time, "sleep", lambda _s: None)

    tray.stop_daemon(show_diary_dialog=True)

    _assert_reads_as_a_stop(tray)
    assert tray.daemon_thread is None


def test_a_second_stop_during_a_stop_is_ignored(qapp, monkeypatch):
    """Stop or Quit clicked while the tray is still waiting for the daemon to
    save its diary does not start a second wait over the same process."""
    import desktop_app.app as app_mod

    tray = _bare_tray(app_mod)
    tray.is_bundled = False
    tray.is_listening = True
    tray.daemon_thread = None

    exited = {"value": False}
    proc = MagicMock()
    proc.poll.side_effect = lambda: 0 if exited["value"] else None
    tray.daemon_process = proc

    pumps = {"n": 0}

    def process_events():
        pumps["n"] += 1
        if pumps["n"] == 2:
            tray.stop_daemon(show_diary_dialog=True)  # the user clicks Stop again
        if pumps["n"] == 4:
            exited["value"] = True

    tray.app = MagicMock()
    tray.app.processEvents.side_effect = process_events
    monkeypatch.setattr(app_mod, "DiaryUpdateDialog", MagicMock())
    monkeypatch.setattr(app_mod.time, "sleep", lambda _s: None)

    tray.stop_daemon(show_diary_dialog=True)

    _assert_reads_as_a_stop(tray)
    toasts, _logs, _statuses = _outcome(tray)
    assert toasts.count("Voice assistant is no longer listening") == 1
