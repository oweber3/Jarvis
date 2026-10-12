"""Desktop side of document search: the tray's rebuild and confirmed delete, the IPC lines and the Settings
rows. Rendered offscreen only; the confirmation dialog is replaced by a test double."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.unit


@pytest.fixture
def menu(qapp):
    from desktop_app.documents_menu import DocumentsMenu
    asked = []

    def confirm(text):
        asked.append(text)
        return confirm.reply

    confirm.reply = True
    m = DocumentsMenu(confirm=confirm)
    m.confirm_double = confirm
    m.asked = asked
    return m


class TestTrayItems:
    def test_rebuild_needs_document_search_on_but_delete_is_always_available(self, menu):
        menu.set_state(enabled=False)
        assert not menu.rebuild_action.isEnabled() and "Settings" in menu.rebuild_action.text()
        assert menu.delete_action.isEnabled()
        menu.set_state(enabled=True)
        assert menu.rebuild_action.isEnabled()

    def test_delete_asks_first_and_only_a_yes_deletes(self, menu):
        deleted = []
        menu.delete_confirmed.connect(lambda: deleted.append(1))
        menu.confirm_double.reply = False
        menu.delete_action.trigger()
        assert deleted == []
        menu.confirm_double.reply = True
        menu.delete_action.trigger()
        assert deleted == [1]
        text = menu.asked[0].lower()
        assert "delete" in text and "undone" in text and "document" in text

    def test_rebuild_is_requested(self, menu):
        menu.set_state(enabled=True)
        requested = []
        menu.rebuild_requested.connect(lambda: requested.append(1))
        menu.rebuild_action.trigger()
        assert requested == [1]


class TestRequests:
    def app(self, **kw):
        defaults = dict(is_listening=False, is_bundled=False, daemon_process=None)
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_subprocess_mode_writes_the_protocol_lines(self):
        from desktop_app.documents_menu import send_request
        from jarvis.daemon import DOCUMENTS_IPC_PREFIX

        written = []
        stdin = SimpleNamespace(write=written.append, flush=lambda: None)
        app = self.app(is_listening=True, daemon_process=SimpleNamespace(stdin=stdin))
        for action in ("rebuild", "delete", "stop_sharing"):
            send_request(app, action)
        assert written == [DOCUMENTS_IPC_PREFIX + json.dumps({"action": a}) + "\n"
                           for a in ("rebuild", "delete", "stop_sharing")]

    def test_daemon_handles_lines_off_the_reader_and_ignores_malformed_ones(self, monkeypatch):
        from jarvis import daemon
        ran = []
        monkeypatch.setattr(daemon, "delete_document_index", lambda: ran.append("delete"))
        monkeypatch.setattr(daemon.threading, "Thread",
                            lambda target, **kw: SimpleNamespace(start=lambda: ran.append(("thread", target()))))
        assert daemon.handle_documents_stdin_line(daemon.DOCUMENTS_IPC_PREFIX + '{"action": "delete"}')
        assert ran[0] == "delete" and ran[1][0] == "thread"
        assert daemon.handle_documents_stdin_line(daemon.DOCUMENTS_IPC_PREFIX + "nonsense")
        assert not daemon.handle_documents_stdin_line("__OTHER__:{}")

    def test_without_a_daemon_delete_clears_the_database(self, tmp_path, monkeypatch):
        from desktop_app.documents_menu import send_request
        from jarvis.documents.store import DocumentStore

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"db_path": str(tmp_path / "jarvis.db")}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        store = DocumentStore(str(tmp_path / "jarvis.db"))
        store.replace_file("/a.txt", 1.0, 1, [(None, "hello", [1.0, 0.0])])
        store.close()
        send_request(self.app(), "delete")
        store = DocumentStore(str(tmp_path / "jarvis.db"))
        assert store.stats()["files"] == 0
        store.close()


class TestSettingsRows:
    def test_every_setting_is_on_the_privacy_page(self):
        from desktop_app.settings_window import FIELD_METADATA
        from jarvis.config import get_default_config
        keys = {k for k in get_default_config() if k.startswith("document_search_")}
        exposed = {fm.key: fm for fm in FIELD_METADATA if fm.key.startswith("document_search_")}
        assert keys == set(exposed)
        assert all(fm.category == "privacy" and fm.section == "Document search" for fm in exposed.values())

    def test_cloud_sharing_row_names_the_providers(self):
        from desktop_app.settings_window import FIELD_METADATA
        fm = next(f for f in FIELD_METADATA if f.key == "document_search_share_with_cloud")
        text = fm.description.lower()
        assert fm.field_type == "bool" and "codex" in text and "claude" in text and "off" in text


class TestTrayWiring:
    def test_the_tray_menu_offers_rebuild_and_delete(self, qapp, monkeypatch):
        from PyQt6.QtWidgets import QSystemTrayIcon
        import desktop_app.documents_menu as documents_menu
        from desktop_app.app import JarvisSystemTray

        tray = JarvisSystemTray.__new__(JarvisSystemTray)
        tray.tray_icon = QSystemTrayIcon()
        tray.create_menu()
        texts = [a.text() for a in tray.menu.actions()]
        assert any("Rebuild Document Index" in t for t in texts)
        assert any("Delete Document Index" in t for t in texts)
        sent = []
        monkeypatch.setattr(documents_menu, "send_request", lambda app, action: sent.append(action))
        tray.documents_menu.set_state(enabled=True)
        tray.documents_menu.rebuild_action.trigger()
        tray.documents_menu._confirm = lambda text: True
        tray.documents_menu.delete_action.trigger()
        assert sent == ["rebuild", "delete"]

    def test_switching_sharing_off_in_settings_stops_it_at_once(self, monkeypatch):
        import desktop_app.documents_menu as documents_menu
        from desktop_app.app import JarvisSystemTray
        import desktop_app.activity_menu as activity_menu
        sent = []
        monkeypatch.setattr(documents_menu, "send_request", lambda app, action: sent.append(action))
        monkeypatch.setattr(activity_menu, "send_request", lambda app, action: None)
        tray = JarvisSystemTray.__new__(JarvisSystemTray)
        tray._apply_settings_now({"document_search_share_with_cloud": False})
        assert sent == ["stop_sharing"]
