"""Clicking the orb wakes Jarvis exactly as saying his name does."""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestDaemonWakeLine:
    """In subprocess mode the listener lives in the daemon, so the click
    travels over stdin like the chat control lines do."""

    def test_a_wake_line_is_recognised_and_wakes_the_listener(self):
        from jarvis import daemon

        listener = MagicMock()
        with patch.object(daemon, "_global_voice_listener", listener):
            assert daemon.handle_wake_stdin_line(daemon.WAKE_IPC_PREFIX) is True

        listener.toggle_manual_wake.assert_called_once_with()

    def test_an_unrelated_line_is_left_for_other_handlers(self):
        from jarvis import daemon

        assert daemon.handle_wake_stdin_line("SHUTDOWN") is False
        assert daemon.handle_wake_stdin_line('__CHAT_QUERY__:{"text":"hi"}') is False

    def test_a_click_during_a_typed_request_stops_it_instead_of_waking(self):
        import threading
        from jarvis import daemon

        listener = MagicMock()
        cancel = threading.Event()
        with patch.object(daemon, "_global_voice_listener", listener), \
                patch.object(daemon, "_chat_cancel_event", cancel):
            daemon.toggle_manual_wake()

        assert cancel.is_set()
        listener.toggle_manual_wake.assert_not_called()

    def test_a_wake_before_the_listener_exists_is_harmless(self):
        from jarvis import daemon

        with patch.object(daemon, "_global_voice_listener", None):
            assert daemon.handle_wake_stdin_line(daemon.WAKE_IPC_PREFIX) is True
            daemon.toggle_manual_wake()


class TestOrbClick:
    def _orb(self):
        from desktop_app.orb_widget import OrbWidget

        orb = OrbWidget()
        orb.resize(300, 300)
        return orb

    def test_a_left_click_on_the_orb_emits_clicked(self, qapp):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest

        orb = self._orb()
        clicks = []
        orb.clicked.connect(lambda: clicks.append(1))
        QTest.mouseClick(orb, Qt.MouseButton.LeftButton, pos=QPoint(150, 150))

        assert clicks == [1]

    def test_other_buttons_do_not_emit_clicked(self, qapp):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest

        orb = self._orb()
        clicks = []
        orb.clicked.connect(lambda: clicks.append(1))
        QTest.mouseClick(orb, Qt.MouseButton.RightButton, pos=QPoint(150, 150))

        assert clicks == []


class TestWakeJarvis:
    def _tray(self, *, listening=True, bundled=False):
        from desktop_app.app import JarvisSystemTray

        tray = JarvisSystemTray.__new__(JarvisSystemTray)
        tray.is_listening = listening
        tray.is_bundled = bundled
        tray.daemon_process = SimpleNamespace(stdin=io.StringIO())
        return tray

    def test_wakes_the_daemon_subprocess(self, qapp):
        from jarvis.daemon import WAKE_IPC_PREFIX

        tray = self._tray()
        tray.toggle_jarvis()

        assert tray.daemon_process.stdin.getvalue() == f"{WAKE_IPC_PREFIX}\n"

    def test_wakes_the_bundled_daemon_in_process(self, qapp):
        tray = self._tray(bundled=True)
        with patch("jarvis.daemon.toggle_manual_wake") as wake:
            tray.toggle_jarvis()

        wake.assert_called_once_with()

    def test_does_nothing_while_the_assistant_is_stopped(self, qapp):
        tray = self._tray(listening=False)
        tray.toggle_jarvis()

        assert tray.daemon_process.stdin.getvalue() == ""

    def test_a_broken_pipe_does_not_raise(self, qapp):
        tray = self._tray()
        tray.daemon_process = SimpleNamespace(stdin=MagicMock(write=MagicMock(side_effect=OSError)))
        tray.toggle_jarvis()

    def test_the_tray_icon_click_is_not_a_wake(self, qapp):
        from desktop_app.app import JarvisSystemTray

        assert not hasattr(JarvisSystemTray, "on_tray_activated")
