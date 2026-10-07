"""The desktop window around the web chat: the daemon-status page, loading the chat, retrying, and links.

The embedded browser is replaced by a stub with the same two members the window uses, so no Chromium
starts. See ``webchat/webchat.spec.md``, Desktop window.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtWidgets import QWidget

from desktop_app.web_chat_window import WebChatWindow, is_chat_url, web_chat_url

PORT = 8766


class StubView(QWidget):
    loadFinished = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.loaded = []

    def setUrl(self, url: QUrl) -> None:
        self.loaded.append(url.toString())


@pytest.fixture
def window(qapp):
    view = StubView()
    win = WebChatWindow(PORT, "stopped", view_factory=lambda: view, retry_ms=15)
    win.stub = view
    yield win
    win.close()


def pump(qapp, ms=120):
    import time
    end = time.time() + ms / 1000
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.005)


def test_the_chat_address_is_the_loopback_port():
    assert web_chat_url(PORT) == f"http://127.0.0.1:{PORT}/"


class TestDaemonStatusPage:
    @pytest.mark.parametrize("status", ["starting", "stopping", "stopped", "crashed"])
    def test_a_daemon_that_is_not_running_shows_a_status_page_not_the_chat(self, window, status):
        window.set_daemon_status(status)
        assert not window.is_showing_chat()
        assert window.notice_text()
        assert window.stub.loaded == []

    def test_each_state_says_something_different(self, window):
        texts = set()
        for status in ("starting", "stopping", "stopped", "crashed"):
            window.set_daemon_status(status)
            texts.add(window.notice_text())
        assert len(texts) == 4

    def test_a_crash_tells_the_owner_how_to_recover(self, window):
        window.set_daemon_status("crashed")
        assert "tray" in window.notice_text().lower()


class TestLoadingTheChat:
    def test_a_running_daemon_loads_the_chat_once(self, window):
        window.set_daemon_status("running")
        window.set_daemon_status("running")
        assert window.stub.loaded == [web_chat_url(PORT)]

    def test_the_chat_shows_once_the_page_has_loaded(self, window):
        window.set_daemon_status("running")
        assert not window.is_showing_chat()
        window.stub.loadFinished.emit(True)
        assert window.is_showing_chat()

    def test_a_restarted_daemon_reloads_the_page(self, window):
        window.set_daemon_status("running")
        window.stub.loadFinished.emit(True)
        window.set_daemon_status("stopped")
        window.set_daemon_status("running")
        assert len(window.stub.loaded) == 2

    def test_a_page_that_is_not_up_yet_is_retried_until_it_loads(self, window, qapp):
        window.set_daemon_status("running")
        window.stub.loadFinished.emit(False)
        assert not window.is_showing_chat()
        pump(qapp)
        attempts = len(window.stub.loaded)
        assert attempts >= 2
        window.stub.loadFinished.emit(True)
        assert window.is_showing_chat()
        pump(qapp)
        assert len(window.stub.loaded) == attempts  # no more retries once it loaded

    def test_retries_stop_when_the_daemon_stops(self, window, qapp):
        window.set_daemon_status("running")
        window.stub.loadFinished.emit(False)
        window.set_daemon_status("stopped")
        pump(qapp)
        before = len(window.stub.loaded)
        pump(qapp)
        assert len(window.stub.loaded) == before

    def test_a_page_that_never_comes_up_explains_what_to_check(self, window):
        window.set_daemon_status("running")
        window.stub.loadFinished.emit(False)
        assert "settings" in window.notice_text().lower()


class TestWithoutAnEmbeddedBrowser:
    def test_the_window_says_the_chat_opens_in_the_default_browser(self, qapp):
        win = WebChatWindow(PORT, "running", view_factory=lambda: None)
        try:
            assert not win.has_embedded_view
            assert "browser" in win.notice_text().lower()
        finally:
            win.close()


class TestLinks:
    def test_only_the_chat_itself_may_be_shown_in_the_window(self):
        assert is_chat_url(QUrl(f"http://127.0.0.1:{PORT}/"), PORT)
        assert is_chat_url(QUrl(f"http://127.0.0.1:{PORT}/assets/index.js"), PORT)

    @pytest.mark.parametrize("address", [
        "https://example.com/", "http://127.0.0.1:9999/", "http://localhost:8766/", "http://127.0.0.1.evil.example:8766/",
        "file:///C:/Windows/win.ini", "javascript:alert(1)", "http://[::1]:8766/",
    ])
    def test_everything_else_is_not(self, address):
        assert not is_chat_url(QUrl(address), PORT)
