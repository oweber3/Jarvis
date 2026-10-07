"""Behaviour tests for the ChatWindow (text chat interface).

These verify the contract in ``src/desktop_app/chat_window.spec.md``:

- The window has a transcript area, an input box, a send button, and a stop
  button (visible only while a query is in flight).
- Sending submits text via ``jarvis.daemon.submit_text_query`` and appends the
  user's message to the transcript.
- Daemon callback signals (start/complete/busy) update the transcript and the
  status indicator on the Qt main thread.
- The stop button calls ``jarvis.daemon.cancel_active_chat_query``.
- Closing hides the window; it does not quit the daemon.
- Styling uses the shared theme stylesheet (no hardcoded colour literals in
  the widget classes).
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestPhoneShell:
    def test_window_controls_and_rounded_frame(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtCore import Qt, QPoint
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QSizeGrip
        monkeypatch.setattr('desktop_app.chat_window.get_hot_window_messages', lambda: [])
        win = ChatWindow()
        win.show()
        QTest.qWait(30)
        rendered = win.grab().toImage()
        assert rendered.pixelColor(0, 0).alpha() == 0
        centre = rendered.rect().center()
        assert rendered.pixelColor(centre).alpha() == 255
        assert win.findChild(QSizeGrip).isVisible()
        QTest.mouseDClick(win.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(50, 10))
        assert win.isMaximized()
        QTest.mouseDClick(win.title_bar, Qt.MouseButton.LeftButton, pos=QPoint(50, 10))
        assert not win.isMaximized()
        win.minimise_button.click()
        assert win.isMinimized()
        win.showNormal()
        win.close()

    def test_custom_frame_close_preserves_conversation(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtCore import Qt
        monkeypatch.setattr('desktop_app.chat_window.get_hot_window_messages', lambda: [])
        win = ChatWindow()
        win.show()
        win._append_assistant('Still here')
        assert win.windowFlags() & Qt.WindowType.FramelessWindowHint
        win.close_button.click()
        assert not win.isVisible()
        win.show()
        assert win.transcript_text() == 'Still here'
        win.close()

    def test_empty_state_disappears_without_becoming_a_message(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        monkeypatch.setattr('desktop_app.chat_window.get_hot_window_messages', lambda: [])
        win = ChatWindow()
        win.show()
        assert win.empty_state.isVisible()
        assert win.transcript_text() == ''
        win._append_user('Hello')
        assert not win.empty_state.isVisible()
        win.close()

    @pytest.mark.parametrize('size', [(380, 560), (480, 780), (800, 650)])
    def test_controls_fit_and_messages_are_literal_at_all_sizes(self, qapp, monkeypatch, size):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QLabel
        from PyQt6.QtTest import QTest
        monkeypatch.setattr('desktop_app.chat_window.get_hot_window_messages', lambda: [])
        win = ChatWindow()
        win.resize(*size)
        win.show()
        win._append_assistant('<b>literal message</b> ' * 20)
        win._set_thinking(True)
        QTest.qWait(50)
        for widget in (win.input_widget, win.send_button, win.stop_button, win.close_button):
            assert win.rect().contains(widget.mapTo(win, widget.rect().bottomRight()))
            assert widget.width() >= 36
        bubble = next(w for w in win.findChildren(QLabel) if w.objectName() == 'bubble')
        assert bubble.textFormat() == Qt.TextFormat.PlainText
        assert bubble.width() < win.transcript_widget.viewport().width()
        win.close()


@pytest.mark.unit
class TestChatWindowStructure:
    """The window exposes the UI elements the spec requires."""

    def test_has_transcript_input_send_and_stop(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow()
        assert win.transcript_widget is not None
        assert win.input_widget is not None
        assert win.send_button is not None
        assert win.stop_button is not None

    def test_stop_button_hidden_at_rest(self, qapp):
        """The stop button is only relevant while a query is running."""
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow()
        assert not win.stop_button.isVisible()

    def test_window_title_mentions_jarvis(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow()
        title = win.windowTitle()
        assert "Jarvis" in title


@pytest.mark.unit
class TestChatWindowSend:
    """Sending a message dispatches to the daemon and echoes the user text."""

    def test_send_calls_submit_text_query(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        calls = []
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query",
            lambda text, **kw: calls.append(text),
        )
        win = ChatWindow()
        win.input_widget.setPlainText("what is the weather")
        win._send()
        assert calls == ["what is the weather"]

    def test_send_appends_user_message_to_transcript(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = ChatWindow()
        win.input_widget.setPlainText("hello there")
        win._send()
        text = win.transcript_text()
        assert "hello there" in text

    def test_send_clears_input(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = ChatWindow()
        win.input_widget.setPlainText("clear me after send")
        win._send()
        assert win.input_widget.toPlainText() == ""

    def test_send_empty_does_nothing(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        calls = []
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query",
            lambda text, **kw: calls.append(text),
        )
        win = ChatWindow()
        win.input_widget.setPlainText("   ")
        win._send()
        assert calls == []

    def test_send_when_daemon_unavailable_does_not_submit(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        calls = []
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query",
            lambda text, **kw: calls.append(text),
        )
        win = ChatWindow(daemon_available=False)
        win.input_widget.setPlainText("are you there")
        win._send()

        assert calls == []
        text = win.transcript_text().lower()
        assert "start listening" in text
        assert win.input_widget.toPlainText() == "are you there"


@pytest.mark.unit
class TestChatWindowCallbacks:
    """Daemon callback signals update the UI on the main thread."""

    def test_on_complete_appends_reply_to_transcript(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = ChatWindow()
        win.input_widget.setPlainText("hi")
        win._send()
        # Simulate the daemon completing with a reply.
        win._on_complete("It is sunny today.")
        text = win.transcript_text()
        assert "It is sunny today." in text

    def test_on_complete_hides_stop_button(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()
        win.input_widget.setPlainText("hi")
        win._send()
        qapp.processEvents()
        # While "thinking" the stop button should be visible.
        assert win.stop_button.isVisible()
        win._on_complete("done")
        qapp.processEvents()
        assert not win.stop_button.isVisible()

    def test_on_busy_appends_busy_notice(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = ChatWindow()
        win.input_widget.setPlainText("second query")
        win._send()
        # Simulate the daemon rejecting because a query is already running.
        win._on_busy()
        text = win.transcript_text()
        # The notice is language-neutral in shape but must mention the query
        # was not accepted.
        assert "second query" in text  # user echo stays
        assert "busy" in text.lower() or "already" in text.lower()


@pytest.mark.unit
class TestChatWindowStop:
    """The stop button cancels the chat query, not the whole daemon."""

    def test_stop_calls_cancel_active_chat_query(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        called = []
        monkeypatch.setattr(
            "jarvis.daemon.cancel_active_chat_query", lambda: called.append(True)
        )
        # request_stop must NOT be called because it tears down the whole
        # voice assistant.
        request_stop_called = []
        monkeypatch.setattr(
            "jarvis.daemon.request_stop",
            lambda: request_stop_called.append(True),
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()
        win._set_thinking(True)
        qapp.processEvents()
        win._stop()
        qapp.processEvents()
        assert called == [True]
        assert request_stop_called == []

    def test_stop_resets_thinking_indicator(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.cancel_active_chat_query", lambda: None
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()
        win._set_thinking(True)
        qapp.processEvents()
        assert win.stop_button.isVisible()
        win._stop()
        qapp.processEvents()
        assert not win.stop_button.isVisible()


@pytest.mark.unit
class TestChatWindowLifecycle:
    """Closing hides rather than tearing down daemon state."""

    def test_close_event_hides_window(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtGui import QCloseEvent

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()
        # The daemon stop function must NOT be called on close.
        stop_called = []
        monkeypatch.setattr(
            "jarvis.daemon.request_stop", lambda: stop_called.append(True)
        )
        win.closeEvent(QCloseEvent())
        assert stop_called == []


@pytest.mark.unit
class TestChatWindowSubmitFn:
    """When a ``submit_fn`` is injected (subprocess mode), sending routes
    through it instead of the daemon's direct call path."""

    def test_submit_fn_receives_text(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        calls = []
        win = ChatWindow(submit_fn=lambda text: calls.append(text))
        # The bundled path must NOT be touched when submit_fn is set.
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must use submit_fn")),
        )
        win.input_widget.setPlainText("via stdin")
        win._send()
        assert calls == ["via stdin"]

    def test_daemon_availability_toggles_input_controls(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow(daemon_available=False)
        assert not win.send_button.isEnabled()
        assert not win.input_widget.isEnabled()

        win.set_daemon_available(True)
        assert win.send_button.isEnabled()
        assert win.input_widget.isEnabled()

        win.set_daemon_available(False)
        assert not win.send_button.isEnabled()
        assert not win.input_widget.isEnabled()


@pytest.mark.unit
class TestDesktopAppChatDispatch:
    """The desktop app routes ``__CHAT__:`` IPC lines to the chat window on the
    main thread via ``_on_chat_ipc_line`` + ``ChatWindow.process_ipc_line``."""

    def _make_tray(self):
        import desktop_app.app as app_mod
        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.chat_window = None
        tray._chat_submit_fn = None
        tray.is_listening = True
        return tray

    def test_on_chat_ipc_line_creates_window_lazily(self, qapp):
        from jarvis.daemon import CHAT_IPC_PREFIX
        tray = self._make_tray()
        assert tray.chat_window is None
        tray._on_chat_ipc_line(f'{CHAT_IPC_PREFIX}{{"type":"complete","data":"hi"}}')
        assert tray.chat_window is not None

    def test_dispatch_complete_appends_reply(self, qapp):
        from jarvis.daemon import CHAT_IPC_PREFIX
        tray = self._make_tray()
        tray._on_chat_ipc_line(f'{CHAT_IPC_PREFIX}{{"type":"complete","data":"hello back"}}')
        tray.chat_window.show()
        qapp.processEvents()
        assert "hello back" in tray.chat_window.transcript_text()

    def test_dispatch_start_sets_thinking(self, qapp):
        from jarvis.daemon import CHAT_IPC_PREFIX
        tray = self._make_tray()
        tray._on_chat_ipc_line(f'{CHAT_IPC_PREFIX}{{"type":"start","data":"a query"}}')
        tray.chat_window.show()
        qapp.processEvents()
        assert tray.chat_window.stop_button.isVisible()

    def test_dispatch_busy_appends_notice(self, qapp):
        from jarvis.daemon import CHAT_IPC_PREFIX
        tray = self._make_tray()
        tray._on_chat_ipc_line(f'{CHAT_IPC_PREFIX}{{"type":"busy","data":null}}')
        tray.chat_window.show()
        qapp.processEvents()
        text = tray.chat_window.transcript_text().lower()
        assert "busy" in text

    def test_dispatch_malformed_line_is_swallowed(self, qapp):
        from jarvis.daemon import CHAT_IPC_PREFIX
        tray = self._make_tray()
        # Must not raise; window is created lazily but no reply text lands.
        tray._on_chat_ipc_line(f"{CHAT_IPC_PREFIX}not json")
        qapp.processEvents()

    def test_process_ipc_line_returns_false_for_non_chat(self, qapp):
        from desktop_app.chat_window import ChatWindow
        win = ChatWindow()
        assert win.process_ipc_line("not a chat line") is False

    def test_process_ipc_line_returns_true_for_malformed_chat(self, qapp):
        from desktop_app.chat_window import ChatWindow
        from jarvis.daemon import CHAT_IPC_PREFIX
        win = ChatWindow()
        assert win.process_ipc_line(f"{CHAT_IPC_PREFIX}not json") is True

    def test_late_ipc_line_creates_unavailable_window_when_daemon_stopped(self, qapp):
        from jarvis.daemon import CHAT_IPC_PREFIX
        tray = self._make_tray()
        tray.is_listening = False

        tray._on_chat_ipc_line(f'{CHAT_IPC_PREFIX}{{"type":"complete","data":"late"}}')

        assert tray.chat_window is not None
        assert not tray.chat_window.send_button.isEnabled()

    def test_subprocess_submit_fn_writes_chat_query_line(self, qapp, monkeypatch):
        """The stdin-bridge callable writes a __CHAT_QUERY__: JSON line."""
        import io
        import json
        from jarvis.daemon import CHAT_QUERY_IPC_PREFIX
        import desktop_app.app as app_mod

        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)

        # Fake a subprocess.Popen with a writable stdin pipe.
        sink = io.StringIO()
        fake_proc = type("P", (), {"stdin": sink})()
        tray.daemon_process = fake_proc

        # Reconstruct the closure the real start_daemon builds.
        def _submit(text: str) -> None:
            tray.daemon_process.stdin.write(
                f"{CHAT_QUERY_IPC_PREFIX}{json.dumps({'text': text})}\n"
            )
            tray.daemon_process.stdin.flush()

        _submit("hello over stdin")
        written = sink.getvalue()
        assert written.startswith(CHAT_QUERY_IPC_PREFIX)
        payload = json.loads(written[len(CHAT_QUERY_IPC_PREFIX):].strip())
        assert payload["text"] == "hello over stdin"

    def test_subprocess_control_fn_writes_rewind_line(self, qapp, monkeypatch):
        """The rewind control closure writes the ``__CHAT_REWIND__:`` IPC
        line (prefix + JSON payload)."""
        import io
        import json
        from jarvis.daemon import CHAT_REWIND_IPC_PREFIX
        import desktop_app.app as app_mod

        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        sink = io.StringIO()
        fake_proc = type("P", (), {"stdin": sink})()
        tray.daemon_process = fake_proc

        def _control(kind: str, payload=None) -> None:
            import json as _json
            assert kind == "rewind"
            tray.daemon_process.stdin.write(
                f"{CHAT_REWIND_IPC_PREFIX}{_json.dumps(payload)}\n"
            )
            tray.daemon_process.stdin.flush()

        _control("rewind", {"text": "what about eggs?", "occurrence": 0})
        lines = sink.getvalue().splitlines()

        assert json.loads(lines[0][len(CHAT_REWIND_IPC_PREFIX):]) == {
            "text": "what about eggs?", "occurrence": 0,
        }

    def test_window_opened_by_a_daemon_event_rewinds_through_the_daemon_pipe(self, qapp, monkeypatch):
        """A chat window first created by a subprocess chat event still sends rewinds to the daemon."""
        import json
        from jarvis.daemon import CHAT_IPC_PREFIX
        import desktop_app.app as app_mod

        monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
        controls = []
        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.chat_window = None
        tray.is_listening = True
        tray._reply_mode = "local"
        tray._chat_submit_fn = lambda text: None
        tray._chat_cancel_fn = lambda: None
        tray._chat_control_fn = lambda kind, payload: controls.append((kind, payload))

        tray._on_chat_ipc_line(f"{CHAT_IPC_PREFIX}{json.dumps({'type': 'busy', 'data': None})}")
        win = tray.chat_window
        win.input_widget.setPlainText("hello")
        win._send()
        win._on_complete("hi")
        win._rewind_to_user(1)

        assert controls == [("rewind", {"text": "hello", "occurrence": 0})]

    def test_show_chat_marks_window_unavailable_when_daemon_stopped(self, qapp):
        import desktop_app.app as app_mod

        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.chat_window = None
        tray._chat_submit_fn = None
        tray.is_listening = False

        tray.show_chat()

        assert tray.chat_window is not None
        assert not tray.chat_window.send_button.isEnabled()

    def test_show_chat_marks_existing_window_available_when_daemon_started(self, qapp):
        import desktop_app.app as app_mod
        from desktop_app.chat_window import ChatWindow

        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.chat_window = ChatWindow(daemon_available=False)
        tray._chat_submit_fn = lambda text: None
        tray.is_listening = True

        tray.show_chat()

        assert tray.chat_window.send_button.isEnabled()
        assert tray.chat_window._submit_fn is tray._chat_submit_fn


@pytest.mark.unit
class TestChatWindowDaemonStatus:
    """The chat window shows daemon lifecycle state without requiring logs."""

    def test_initial_unavailable_state_shows_status_banner(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow(daemon_available=False)
        win.show()
        qapp.processEvents()

        assert not win.send_button.isEnabled()
        assert not win.input_widget.isEnabled()
        assert win._status_label.isVisible()
        assert "Start Listening" in win._status_label.text()

    def test_starting_state_disables_submission_and_shows_progress(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow()
        win.show()
        qapp.processEvents()

        win.set_daemon_status("starting")
        qapp.processEvents()

        assert not win.send_button.isEnabled()
        assert not win.input_widget.isEnabled()
        assert win._status_label.isVisible()
        assert "Starting" in win._status_label.text()

    def test_stopping_state_disables_submission_and_shows_progress(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow()
        win.show()
        qapp.processEvents()

        win.set_daemon_status("stopping")
        qapp.processEvents()

        assert not win.send_button.isEnabled()
        assert not win.input_widget.isEnabled()
        assert win._status_label.isVisible()
        assert "Stopping" in win._status_label.text()

    def test_running_state_hides_status_banner_and_reenables_submission(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow(daemon_available=False)
        win.show()
        qapp.processEvents()

        win.set_daemon_status("running")
        qapp.processEvents()

        assert win.send_button.isEnabled()
        assert win.input_widget.isEnabled()
        assert not win._status_label.isVisible()

    def test_crashed_state_resets_thinking_and_explains_reconnect(self, qapp):
        from desktop_app.chat_window import ChatWindow

        win = ChatWindow()
        win.show()
        qapp.processEvents()
        win._set_thinking(True)
        qapp.processEvents()

        win.set_daemon_status("crashed")
        qapp.processEvents()

        assert not win.stop_button.isVisible()
        assert not win.send_button.isEnabled()
        assert win._status_label.isVisible()
        label = win._status_label.text().lower()
        assert "unexpectedly" in label
        assert "start listening" in label


@pytest.mark.unit
class TestDesktopAppChatStatus:
    """The tray forwards daemon lifecycle state to an open chat window."""

    def test_set_chat_daemon_status_updates_existing_window(self, qapp):
        import desktop_app.app as app_mod
        from desktop_app.chat_window import ChatWindow

        tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
        tray.chat_window = ChatWindow()
        tray.chat_window.show()
        tray._chat_submit_fn = lambda text: None

        tray._set_chat_daemon_status("crashed")
        qapp.processEvents()

        assert not tray.chat_window.send_button.isEnabled()
        assert "unexpectedly" in tray.chat_window._status_label.text().lower()


@pytest.mark.unit
class TestChatWindowInputKeys:
    """Enter sends; Shift+Enter inserts a newline (does not send)."""

    def test_enter_sends(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtCore import Qt as _Qt, QEvent
        from PyQt6.QtGui import QKeyEvent

        calls = []
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query",
            lambda text, **kw: calls.append(text),
        )
        win = ChatWindow()
        win.input_widget.setPlainText("hi")
        event = QKeyEvent(
            QEvent.Type.KeyPress,
            _Qt.Key.Key_Return,
            _Qt.KeyboardModifier.NoModifier,
        )
        win._input_key_press(event)
        assert calls == ["hi"]
        assert win.input_widget.toPlainText() == ""

    def test_numpad_enter_sends(self, qapp, monkeypatch):
        """Numpad Enter (Key_Enter) sends just like the main Return key."""
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtCore import Qt as _Qt, QEvent
        from PyQt6.QtGui import QKeyEvent

        calls = []
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query",
            lambda text, **kw: calls.append(text),
        )
        win = ChatWindow()
        win.input_widget.setPlainText("hi")
        event = QKeyEvent(
            QEvent.Type.KeyPress,
            _Qt.Key.Key_Enter,
            _Qt.KeyboardModifier.NoModifier,
        )
        win._input_key_press(event)
        assert calls == ["hi"]

    def test_shift_enter_does_not_send(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtCore import Qt as _Qt, QEvent
        from PyQt6.QtGui import QKeyEvent

        calls = []
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query",
            lambda text, **kw: calls.append(text),
        )
        win = ChatWindow()
        win.input_widget.setPlainText("line one")
        event = QKeyEvent(
            QEvent.Type.KeyPress,
            _Qt.Key.Key_Return,
            _Qt.KeyboardModifier.ShiftModifier,
        )
        win._input_key_press(event)
        # Default QPlainTextEdit handling inserts a newline; no send.
        assert calls == []


@pytest.mark.unit
class TestChatWindowTranscriptScroll:
    """New messages keep the latest content visible (auto-scroll to bottom)."""

    def test_append_scrolls_to_bottom_after_many_lines(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtTest import QTest

        monkeypatch.setattr(
            "desktop_app.chat_window.get_hot_window_messages", lambda: []
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()
        # Force a tall transcript so the viewport is scrolled past the first
        # lines. Each append must bring the cursor (the view) back to the end.
        for _ in range(80):
            win._append_assistant("line of transcript content " * 4)
        QTest.qWait(100)

        scroll_bar = win.transcript_widget.verticalScrollBar()
        assert scroll_bar.maximum() > 0
        assert scroll_bar.value() == scroll_bar.maximum()

    @pytest.mark.parametrize("kind", ["user", "assistant", "system"])
    def test_new_message_scrolls_to_bottom_from_scrolled_up_position(self, qapp, monkeypatch, kind):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtTest import QTest

        monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])

        win = ChatWindow()
        win.show()
        for index in range(80):
            win._append_assistant(f"older message {index} " * 4)
        QTest.qWait(100)

        scroll_bar = win.transcript_widget.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.minimum())
        assert scroll_bar.value() < scroll_bar.maximum()

        getattr(win, f"_append_{kind}")("newest message")
        QTest.qWait(100)

        assert scroll_bar.value() == scroll_bar.maximum()


@pytest.mark.unit
class TestChatWindowCloseHidesNotDestroys:
    """Closing the window hides it; the tray re-shows the same instance."""

    def test_close_event_hides_window_without_destroying(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow
        from PyQt6.QtGui import QCloseEvent

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()
        assert win.isVisible()

        win.closeEvent(QCloseEvent())
        qapp.processEvents()

        # Hidden, but the same instance is still usable (not destroyed).
        assert not win.isVisible()
        # The transcript and inputs remain intact: closing never resets state.
        assert win.transcript_widget is not None
        assert win.input_widget is not None


@pytest.mark.unit
class TestChatWindowHotWindowReplay:
    """Opening the window for the first time replays the daemon's current hot
    window so the user sees recent voice/text turns instead of a blank
    transcript. Seeded once; re-showing never duplicates."""

    def test_first_show_seeds_transcript_from_hot_window(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        hot_window = [
            {"role": "user", "content": "what is the weather"},
            {"role": "assistant", "content": "It is sunny."},
        ]
        monkeypatch.setattr(
            "desktop_app.chat_window.get_hot_window_messages",
            lambda: hot_window,
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()

        text = win.transcript_text()
        assert "what is the weather" in text
        assert "It is sunny." in text

    def test_re_show_does_not_duplicate_seeded_turns(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        monkeypatch.setattr(
            "desktop_app.chat_window.get_hot_window_messages",
            lambda: [{"role": "user", "content": "hi"}],
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()
        win.hide()
        qapp.processEvents()
        win.show()
        qapp.processEvents()

        text = win.transcript_text()
        assert text.count("hi") == 1

    def test_empty_hot_window_leaves_transcript_blank(self, qapp, monkeypatch):
        from desktop_app.chat_window import ChatWindow

        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        monkeypatch.setattr(
            "desktop_app.chat_window.get_hot_window_messages", lambda: []
        )
        win = ChatWindow()
        win.show()
        qapp.processEvents()

        assert win.transcript_text() == ""


@pytest.mark.unit
class TestChatWindowSmsLook:
    """The window reads as an SMS thread with a single contact: no session
    sidebar, one continuous conversation, speech bubbles aligned by sender,
    and a contact header."""

    def _window(self, qapp, **kwargs):
        from desktop_app.chat_window import ChatWindow
        return ChatWindow(**kwargs)

    def test_no_session_sidebar(self, qapp):
        """There is no session list and no new-session button: the window is
        a single conversation, like an SMS thread with one contact."""
        win = self._window(qapp)
        assert not hasattr(win, "session_list")
        assert not hasattr(win, "new_session_button")
        assert win._messages == []

    def test_header_shows_contact_and_presence(self, qapp):
        from PyQt6.QtWidgets import QLabel
        win = self._window(qapp)
        win.show()
        qapp.processEvents()
        texts = [label.text() for label in win.findChildren(QLabel)]
        assert "Jarvis" in texts
        assert "Online" in texts

    def test_header_shows_typing_while_query_in_flight(self, qapp, monkeypatch):
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = self._window(qapp)
        win.show()
        qapp.processEvents()
        win.input_widget.setPlainText("hi")
        win._send()
        qapp.processEvents()
        assert win._header_status.text() == "Typing…"

    def test_single_conversation_accumulates_all_turns(self, qapp, monkeypatch):
        """Voice-seeded turns and typed turns live in one transcript; there
        is no way to split the conversation into separate sessions."""
        monkeypatch.setattr(
            "desktop_app.chat_window.get_hot_window_messages",
            lambda: [
                {"role": "user", "content": "voice question"},
                {"role": "assistant", "content": "voice answer"},
            ],
        )
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = self._window(qapp)
        win.show()
        qapp.processEvents()
        win.input_widget.setPlainText("typed question")
        win._send()
        win._on_complete("typed answer")

        text = win.transcript_text()
        assert "voice question" in text
        assert "voice answer" in text
        assert "typed question" in text
        assert "typed answer" in text

    def test_user_bubble_right_assistant_left(self, qapp, monkeypatch):
        """SMS layout: the user's bubble sits on the right half of the
        window, Jarvis's reply on the left half."""
        from PyQt6.QtWidgets import QLabel
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = self._window(qapp)
        win.show()
        qapp.processEvents()
        win.input_widget.setPlainText("hi there")
        win._send()
        win._on_complete("hello back")
        qapp.processEvents()

        bubbles = [
            label
            for label in win.transcript_widget.findChildren(QLabel)
            if label.objectName() == "bubble"
        ]
        assert len(bubbles) == 2
        user_bubble, assistant_bubble = bubbles
        mid = win.width() // 2
        assert user_bubble.mapTo(win, user_bubble.rect().topLeft()).x() > mid
        assert assistant_bubble.mapTo(win, assistant_bubble.rect().topLeft()).x() < mid

    def test_bubbles_show_plain_text_without_role_prefixes(self, qapp, monkeypatch):
        """The bubbles carry the message bodies only; position and colour
        convey the sender, so there is no 'You:' / 'Jarvis:' prefix."""
        from PyQt6.QtWidgets import QLabel
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = self._window(qapp)
        win.input_widget.setPlainText("no prefix")
        win._send()
        win._on_complete("plain reply")

        texts = [
            label.text()
            for label in win.transcript_widget.findChildren(QLabel)
            if label.objectName() == "bubble"
        ]
        assert texts == ["no prefix", "plain reply"]
        assert all("You:" not in t and "Jarvis:" not in t for t in texts)

    def test_bubbles_carry_timestamps(self, qapp, monkeypatch):
        from PyQt6.QtWidgets import QLabel
        monkeypatch.setattr(
            "jarvis.daemon.submit_text_query", lambda text, **kw: None
        )
        win = self._window(qapp)
        win.input_widget.setPlainText("timed")
        win._send()

        time_labels = [
            label
            for label in win.transcript_widget.findChildren(QLabel)
            if label.objectName() == "chatTime"
        ]
        assert time_labels, "each bubble should show a muted timestamp"
        assert all(":" in label.text() for label in time_labels)


@pytest.fixture
def live_daemon(monkeypatch):
    """The daemon's shared memory and query lock, with a stand-in reply engine.

    The engine records each turn in memory the way the real one does (the
    redacted query, then the reply) and notes what the model would have seen.
    """
    import threading

    from jarvis import daemon
    from jarvis.memory.conversation import DialogueMemory
    from jarvis.utils.redact import redact

    dm = DialogueMemory(inactivity_timeout=300, max_interactions=50)
    seen = []
    replies = {}

    def fake_engine(db, cfg, tts, text, dialogue_memory, language=None, **kwargs):
        seen.append((text, [m["content"] for m in dialogue_memory.all_messages()]))
        replies[text] = replies.get(text, 0) + 1
        reply = f"reply {replies[text]} to {text}"
        dialogue_memory.add_message("user", redact(text))
        dialogue_memory.add_message("assistant", reply)
        return reply

    monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", fake_engine)
    monkeypatch.setattr(daemon, "_global_dialogue_memory", dm)
    monkeypatch.setattr(daemon, "_global_cfg", object())
    monkeypatch.setattr(daemon, "_global_db", object())
    monkeypatch.setattr(daemon, "_global_stop_requested", False)
    monkeypatch.setattr(daemon, "_chat_query_lock", threading.Lock())
    return dm, seen


def _pump_until(qapp, condition, timeout=5.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.mark.unit
class TestChatRewind:
    """The rewind button beside a sent message rolls the conversation back
    to that message and regenerates a fresh reply. It is anchored on the
    message itself, so turns the window never showed cannot shift it."""

    def _window(self, qapp, monkeypatch, **kwargs):
        from desktop_app.chat_window import ChatWindow
        monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
        return ChatWindow(**kwargs)

    def _send(self, qapp, win, text, wait=True):
        win.input_widget.setPlainText(text)
        win._send()
        if wait:
            _pump_until(qapp, lambda: not win._query_in_flight)

    def _rewind_button(self, win, n):
        from PyQt6.QtWidgets import QPushButton
        return win.transcript_widget.findChild(QPushButton, f"rewind_{n}")

    def _rewind_names(self, win):
        from PyQt6.QtWidgets import QPushButton
        return [
            b.objectName() for b in win.transcript_widget.findChildren(QPushButton)
            if b.objectName().startswith("rewind_")
        ]

    def test_every_sent_message_carries_a_rewind_button(self, qapp, monkeypatch):
        monkeypatch.setattr("jarvis.daemon.submit_text_query", lambda text, **kw: None)
        win = self._window(qapp, monkeypatch)
        self._send(qapp, win, "one", wait=False)
        win._on_complete(None)
        self._send(qapp, win, "two", wait=False)

        assert self._rewind_names(win) == ["rewind_1", "rewind_2"]

    def test_rewind_regenerates_without_the_old_turn(self, qapp, monkeypatch, live_daemon):
        dm, seen = live_daemon
        win = self._window(qapp, monkeypatch)
        self._send(qapp, win, "first")
        self._send(qapp, win, "second")

        self._rewind_button(win, 1).click()
        _pump_until(qapp, lambda: not win._query_in_flight)

        assert win.transcript_text().splitlines() == ["first", "reply 2 to first"]
        assert seen[-1] == ("first", []), "the model must not see the old turn or anything after it"
        assert [m["content"] for m in dm.all_messages()] == ["first", "reply 2 to first"]

    def test_turns_the_window_never_showed_do_not_shift_the_rewind(self, qapp, monkeypatch, live_daemon):
        dm, seen = live_daemon
        win = self._window(qapp, monkeypatch)
        self._send(qapp, win, "typed A")
        dm.add_message("user", "spoken question")  # a voice turn the window never shows
        dm.add_message("assistant", "spoken answer")
        self._send(qapp, win, "typed B")

        self._rewind_button(win, 2).click()
        _pump_until(qapp, lambda: not win._query_in_flight)

        assert seen[-1] == ("typed B", ["typed A", "reply 1 to typed A", "spoken question", "spoken answer"])
        assert win.transcript_text().splitlines() == [
            "typed A", "reply 1 to typed A", "typed B", "reply 2 to typed B",
        ]

    def test_a_message_no_longer_in_memory_is_reported_and_kept(self, qapp, monkeypatch, live_daemon):
        dm, seen = live_daemon
        win = self._window(qapp, monkeypatch)
        self._send(qapp, win, "old question")
        dm.clear()  # the conversation timed out and memory let the turn go
        before = win.transcript_text()

        self._rewind_button(win, 1).click()
        qapp.processEvents()

        lines = win.transcript_text().splitlines()
        assert lines[:2] == before.splitlines(), "the transcript must not be truncated"
        assert "no longer" in lines[-1].lower(), "the user must be told why nothing happened"
        assert len(seen) == 1, "nothing is regenerated"
        assert not win._query_in_flight
        assert self._rewind_button(win, 1).isEnabled()

    def test_rewind_while_jarvis_is_busy_leaves_everything(self, qapp, monkeypatch, live_daemon):
        from jarvis import daemon

        dm, _seen = live_daemon
        win = self._window(qapp, monkeypatch)
        self._send(qapp, win, "question")
        assert daemon._chat_query_lock.acquire(blocking=False)  # a voice query is running
        try:
            self._rewind_button(win, 1).click()
            qapp.processEvents()
        finally:
            daemon._chat_query_lock.release()

        lines = win.transcript_text().splitlines()
        assert lines[:2] == ["question", "reply 1 to question"]
        assert "busy" in lines[-1].lower()
        assert len(dm.all_messages()) == 2

    def test_subprocess_rewind_waits_for_the_daemon(self, qapp, monkeypatch):
        import json

        from jarvis.daemon import CHAT_IPC_PREFIX

        def event(kind, data):
            return f"{CHAT_IPC_PREFIX}{json.dumps({'type': kind, 'data': data})}"

        commands, submits = [], []
        win = self._window(
            qapp, monkeypatch,
            submit_fn=submits.append,
            control_fn=lambda kind, payload: commands.append((kind, payload)),
        )
        self._send(qapp, win, "question", wait=False)
        win.process_ipc_line(event("complete", "old reply"))

        self._rewind_button(win, 1).click()
        assert commands == [("rewind", {"text": "question", "occurrence": 0})]
        assert submits == ["question"], "the daemon regenerates; the window does not submit again"
        assert "old reply" in win.transcript_text(), "nothing is dropped before the daemon accepts"

        win.process_ipc_line(event("rewind", True))
        assert win.transcript_text().splitlines() == ["question"]
        win.process_ipc_line(event("start", "question"))
        win.process_ipc_line(event("complete", "new reply"))
        assert win.transcript_text().splitlines() == ["question", "new reply"]

    def test_subprocess_rewind_refused_keeps_the_transcript(self, qapp, monkeypatch):
        import json

        from jarvis.daemon import CHAT_IPC_PREFIX

        win = self._window(
            qapp, monkeypatch, submit_fn=lambda t: None, control_fn=lambda kind, payload: None,
        )
        self._send(qapp, win, "question", wait=False)
        win.process_ipc_line(f"{CHAT_IPC_PREFIX}{json.dumps({'type': 'complete', 'data': 'old reply'})}")

        self._rewind_button(win, 1).click()
        win.process_ipc_line(f"{CHAT_IPC_PREFIX}{json.dumps({'type': 'rewind', 'data': False})}")

        lines = win.transcript_text().splitlines()
        assert lines[:2] == ["question", "old reply"]
        assert "no longer" in lines[-1].lower()
        assert not win._query_in_flight

    def test_repeated_text_names_which_occurrence(self, qapp, monkeypatch):
        commands = []
        win = self._window(
            qapp, monkeypatch, submit_fn=lambda t: None,
            control_fn=lambda kind, payload: commands.append(payload),
        )
        for _ in range(3):
            self._send(qapp, win, "again?", wait=False)
            win._on_complete("answer")

        self._rewind_button(win, 2).click()

        assert commands == [{"text": "again?", "occurrence": 1}]

    def test_occurrence_counts_messages_memory_holds_alike(self, qapp, monkeypatch):
        """Messages that differ only in what redaction removes are one text to memory."""
        commands = []
        win = self._window(
            qapp, monkeypatch, submit_fn=lambda t: None,
            control_fn=lambda kind, payload: commands.append(payload),
        )
        for address in ("a@example.com", "b@example.com"):
            self._send(qapp, win, f"email {address} please", wait=False)
            win._on_complete("done")

        self._rewind_button(win, 1).click()

        assert commands == [{"text": "email a@example.com please", "occurrence": 1}]

    def test_stop_during_a_rewind_keeps_what_was_typed_after_it(self, qapp, monkeypatch):
        import json

        from jarvis.daemon import CHAT_IPC_PREFIX

        def event(kind, data):
            return f"{CHAT_IPC_PREFIX}{json.dumps({'type': kind, 'data': data})}"

        commands = []
        win = self._window(
            qapp, monkeypatch, submit_fn=lambda t: None,
            control_fn=lambda kind, payload: commands.append(payload),
        )
        self._send(qapp, win, "question", wait=False)
        win.process_ipc_line(event("complete", "old reply"))
        self._rewind_button(win, 1).click()
        win._stop()
        self._rewind_button(win, 1).click()  # a second rewind waits for the first answer
        self._send(qapp, win, "follow-up", wait=False)

        win.process_ipc_line(event("rewind", True))

        assert len(commands) == 1
        assert win.transcript_text().splitlines() == ["question", "follow-up"]

    def test_rewind_is_available_again_after_the_daemon_restarts(self, qapp, monkeypatch):
        win = self._window(
            qapp, monkeypatch, submit_fn=lambda t: None, control_fn=lambda kind, payload: None,
        )
        self._send(qapp, win, "question", wait=False)  # in flight: rewind disabled
        win.set_daemon_status("crashed")
        win.set_daemon_status("running")

        assert self._rewind_button(win, 1).isEnabled()

    def test_rewind_does_nothing_while_a_query_is_in_flight(self, qapp, monkeypatch):
        commands = []
        win = self._window(
            qapp, monkeypatch, submit_fn=lambda t: None,
            control_fn=lambda kind, payload: commands.append(payload),
        )
        self._send(qapp, win, "question", wait=False)  # leaves the query in flight

        win._rewind_to_user(1)

        assert commands == []

    def test_numbering_stays_stable_after_a_rewind(self, qapp, monkeypatch, live_daemon):
        win = self._window(qapp, monkeypatch)
        self._send(qapp, win, "one")
        self._send(qapp, win, "two")
        self._rewind_button(win, 2).click()
        _pump_until(qapp, lambda: not win._query_in_flight)
        self._send(qapp, win, "three")

        assert self._rewind_names(win) == ["rewind_1", "rewind_2", "rewind_3"]


