"""The tray's Chat entry opens the web chat when it is turned on, and the classic chat otherwise."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import desktop_app.app as app_mod


class FakeWebChatWindow:
    created = []

    def __init__(self, port, daemon_status="stopped", **_kwargs):
        self.port = port
        self.statuses = [daemon_status]
        self.shown = 0
        self.has_embedded_view = True
        self.url = f"http://127.0.0.1:{port}/"
        FakeWebChatWindow.created.append(self)

    def set_daemon_status(self, status):
        self.statuses.append(status)

    def show(self):
        self.shown += 1

    def raise_(self):
        pass

    def activateWindow(self):
        pass


@pytest.fixture(autouse=True)
def fake_window(monkeypatch):
    FakeWebChatWindow.created = []
    monkeypatch.setattr("desktop_app.web_chat_window.WebChatWindow", FakeWebChatWindow)


def make_tray(port, *, listening=True):
    tray = app_mod.JarvisSystemTray.__new__(app_mod.JarvisSystemTray)
    tray.chat_window = None
    tray.web_chat_window = None
    tray._web_chat_port = port
    tray._chat_submit_fn = None
    tray._reply_mode = "local"
    tray.is_listening = listening
    return tray


@pytest.mark.unit
class TestChatEntry:
    def test_the_web_chat_opens_when_it_is_turned_on(self, qapp):
        tray = make_tray(8766)
        tray.show_chat()
        assert len(FakeWebChatWindow.created) == 1 and FakeWebChatWindow.created[0].port == 8766
        assert tray.chat_window is None
        assert FakeWebChatWindow.created[0].shown == 1

    def test_the_window_starts_in_the_daemons_state(self, qapp):
        make_tray(8766, listening=True).show_chat()
        make_tray(8766, listening=False).show_chat()
        assert [w.statuses[0] for w in FakeWebChatWindow.created] == ["running", "stopped"]

    def test_the_same_window_is_reused(self, qapp):
        tray = make_tray(8766)
        tray.show_chat()
        tray.show_chat()
        assert len(FakeWebChatWindow.created) == 1
        assert FakeWebChatWindow.created[0].shown == 2

    def test_the_classic_chat_opens_when_the_web_chat_is_off(self, qapp):
        tray = make_tray(None, listening=False)
        tray.show_chat()
        assert FakeWebChatWindow.created == []
        assert tray.chat_window is not None

    def test_daemon_state_changes_reach_the_web_chat_window(self, qapp):
        tray = make_tray(8766)
        tray.show_chat()
        tray._set_chat_daemon_status("stopped")
        tray._set_chat_daemon_status("running")
        assert FakeWebChatWindow.created[0].statuses[-2:] == ["stopped", "running"]

    def test_a_missing_embedded_browser_opens_the_default_browser(self, qapp, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        tray = make_tray(8766)
        tray.web_chat_window = None
        monkeypatch.setattr(FakeWebChatWindow, "__init__",
                            lambda self, port, status="stopped", **kw: (
                                setattr(self, "has_embedded_view", False), setattr(self, "shown", 0),
                                setattr(self, "url", f"http://127.0.0.1:{port}/"),
                                setattr(self, "statuses", [status]), None)[-1])
        tray.show_chat()
        assert opened == ["http://127.0.0.1:8766/"]


@pytest.mark.unit
class TestReadingTheSetting:
    def test_off_means_no_port(self, monkeypatch):
        monkeypatch.setattr("jarvis.config.load_settings",
                            lambda: type("S", (), {"web_chat_enabled": False, "web_chat_port": 8766})())
        assert app_mod.web_chat_port_if_enabled() is None

    def test_on_gives_the_configured_port(self, monkeypatch):
        monkeypatch.setattr("jarvis.config.load_settings",
                            lambda: type("S", (), {"web_chat_enabled": True, "web_chat_port": 9200})())
        assert app_mod.web_chat_port_if_enabled() == 9200

    def test_an_unreadable_configuration_means_off(self, monkeypatch):
        def boom():
            raise OSError

        monkeypatch.setattr("jarvis.config.load_settings", boom)
        assert app_mod.web_chat_port_if_enabled() is None
