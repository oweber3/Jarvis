"""Desktop side of the activity log: the tray's pause toggle and confirmed delete, the IPC lines and the
Settings rows. Rendered offscreen only; the confirmation dialog is replaced by a test double."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.unit


@pytest.fixture
def menu(qapp):
    from desktop_app.activity_menu import ActivityMenu
    answers = []

    def confirm(text):
        answers.append(text)
        return confirm.reply

    confirm.reply = True
    m = ActivityMenu(confirm=confirm)
    m.confirm_double = confirm
    m.asked = answers
    return m


class TestTrayItems:
    def test_pause_is_a_toggle_that_follows_the_stored_state(self, menu):
        menu.set_state(enabled=True, paused=False)
        assert menu.pause_action.isCheckable() and menu.pause_action.isEnabled()
        assert not menu.pause_action.isChecked()
        menu.set_state(enabled=True, paused=True)
        assert menu.pause_action.isChecked()

    def test_pause_is_unavailable_while_the_log_is_off_but_delete_stays_available(self, menu):
        menu.set_state(enabled=False, paused=False)
        assert not menu.pause_action.isEnabled()
        assert "Settings" in menu.pause_action.text()
        assert menu.delete_action.isEnabled()  # old history can still be removed

    def test_toggling_requests_pause_then_resume(self, menu):
        menu.set_state(enabled=True, paused=False)
        requests = []
        menu.pause_toggled.connect(requests.append)
        menu.pause_action.trigger()
        menu.pause_action.trigger()
        assert requests == [True, False]

    def test_delete_asks_first_and_only_a_yes_deletes(self, menu):
        deleted = []
        menu.delete_confirmed.connect(lambda: deleted.append(1))
        menu.confirm_double.reply = False
        menu.delete_action.trigger()
        assert deleted == [] and len(menu.asked) == 1
        menu.confirm_double.reply = True
        menu.delete_action.trigger()
        assert deleted == [1]

    def test_the_question_says_what_is_deleted_and_that_it_cannot_be_undone(self, menu):
        menu.delete_action.trigger()
        text = menu.asked[0].lower()
        assert "delete" in text and "undone" in text and "window title" in text

    def test_the_real_dialog_defaults_to_no(self, qapp, monkeypatch):
        from PyQt6.QtWidgets import QMessageBox
        from desktop_app import activity_menu

        captured = {}

        def fake_question(parent, title, text, buttons, default):
            captured.update(buttons=buttons, default=default)
            return QMessageBox.StandardButton.No

        monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
        assert activity_menu.confirm_delete("Delete everything?") is False
        assert captured["default"] == QMessageBox.StandardButton.No

    def test_items_are_added_to_a_menu(self, menu, qapp):
        from PyQt6.QtWidgets import QMenu
        target = QMenu()
        menu.add_to(target)
        texts = [a.text() for a in target.actions()]
        assert any("Pause" in t for t in texts) and any("Delete" in t and "…" in t for t in texts)


class TestStoredState:
    def test_reads_enabled_and_paused_from_settings(self, tmp_path, monkeypatch):
        from desktop_app.activity_menu import configured_state
        path = tmp_path / "config.json"
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        path.write_text("{}")
        assert configured_state() == (False, False)
        path.write_text(json.dumps({"activity_log_enabled": True, "activity_log_paused": True}))
        assert configured_state() == (True, True)


class TestRequests:
    def app(self, **kw):
        defaults = dict(is_listening=False, is_bundled=False, daemon_process=None)
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_subprocess_mode_writes_the_protocol_lines(self):
        from desktop_app.activity_menu import request_line, send_request
        from jarvis.daemon import ACTIVITY_IPC_PREFIX

        written = []
        stdin = SimpleNamespace(write=written.append, flush=lambda: None)
        app = self.app(is_listening=True, daemon_process=SimpleNamespace(stdin=stdin))
        for action in ("pause", "resume", "delete"):
            send_request(app, action)
        assert written == [ACTIVITY_IPC_PREFIX + json.dumps({"action": a}) + "\n"
                           for a in ("pause", "resume", "delete")]
        assert request_line("pause") == ACTIVITY_IPC_PREFIX + json.dumps({"action": "pause"})

    def test_bundled_mode_calls_the_daemon_off_the_gui_thread(self, monkeypatch):
        import threading
        from desktop_app.activity_menu import send_request
        from jarvis import daemon

        seen = []
        done = threading.Event()
        gui = threading.current_thread()
        monkeypatch.setattr(daemon, "set_activity_paused",
                            lambda paused: (seen.append((paused, threading.current_thread())), done.set()))
        monkeypatch.setattr(daemon, "delete_activity_history",
                            lambda: (seen.append(("delete", threading.current_thread())), done.set()))
        app = self.app(is_listening=True, is_bundled=True)
        send_request(app, "pause")
        assert done.wait(2)
        done.clear()
        send_request(app, "delete")
        assert done.wait(2)
        assert [s[0] for s in seen] == [True, "delete"] and all(s[1] is not gui for s in seen)

    def test_without_a_daemon_pause_is_stored_and_delete_clears_the_database(self, tmp_path, monkeypatch):
        from desktop_app.activity_menu import send_request
        from jarvis.config import load_settings
        from jarvis.memory.activity_log import ActivityStore

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"db_path": str(tmp_path / "jarvis.db")}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        store = ActivityStore(str(tmp_path / "jarvis.db"))
        store.open_session(1.0, "p", "P", "t", end=5.0)
        store.close()

        app = self.app()
        send_request(app, "pause")
        assert load_settings().activity_log_paused is True
        send_request(app, "resume")
        assert load_settings().activity_log_paused is False
        send_request(app, "delete")
        store = ActivityStore(str(tmp_path / "jarvis.db"))
        assert store.sessions(0, 10) == []
        store.close()


class TestSettingsRows:
    def field(self, key):
        from desktop_app.settings_window import FIELD_METADATA
        return next(f for f in FIELD_METADATA if f.key == key)

    def test_every_activity_setting_is_exposed_on_its_own_page(self):
        from desktop_app.settings_window import CATEGORIES, FIELD_METADATA
        from jarvis.config import get_default_config

        keys = {k for k in get_default_config() if k.startswith("activity_log_")}
        exposed = {fm.key: fm for fm in FIELD_METADATA if fm.key.startswith("activity_log_")}
        assert keys == set(exposed)
        assert all(fm.category == "activity" for fm in exposed.values())
        assert "activity" in {key for key, _ in CATEGORIES}

    def test_the_toggle_says_exactly_what_is_recorded_and_where_it_stays(self):
        fm = self.field("activity_log_enabled")
        text = fm.description.lower()
        assert fm.field_type == "bool"
        for word in ("application", "window title", "off", "this pc"):
            assert word in text, word

    def test_cloud_sharing_is_a_separate_off_by_default_switch(self):
        fm = self.field("activity_log_share_with_cloud")
        assert fm.field_type == "bool"
        text = fm.description.lower()
        assert "codex" in text and "claude" in text and "off" in text

    @pytest.mark.parametrize("key", ["activity_log_excluded_processes", "activity_log_private_title_markers"])
    def test_exclusion_lists_are_editable(self, key):
        assert self.field(key).field_type == "list"


class TestTrayWiring:
    def tray(self, qapp):
        from PyQt6.QtWidgets import QSystemTrayIcon
        from desktop_app.app import JarvisSystemTray

        tray = JarvisSystemTray.__new__(JarvisSystemTray)
        tray.tray_icon = QSystemTrayIcon()
        tray.create_menu()
        return tray

    def test_the_tray_menu_offers_pause_and_delete(self, qapp):
        texts = [a.text() for a in self.tray(qapp).menu.actions()]
        assert any("Pause Activity Log" in t for t in texts)
        assert any("Delete Activity History" in t for t in texts)

    def test_menu_choices_reach_the_request_path(self, qapp, monkeypatch):
        import desktop_app.activity_menu as activity_menu

        tray = self.tray(qapp)
        sent = []
        monkeypatch.setattr(activity_menu, "send_request", lambda app, action: sent.append((app, action)))
        tray.activity_menu.set_state(enabled=True, paused=False)
        tray.activity_menu.pause_action.trigger()
        tray.activity_menu.pause_action.trigger()
        tray.activity_menu._confirm = lambda text: True
        tray.activity_menu.delete_action.trigger()
        assert [a for _, a in sent] == ["pause", "resume", "delete"]
        assert all(app is tray for app, _ in sent)

    def test_the_stored_state_is_shown_each_time_the_menu_opens(self, qapp, tmp_path, monkeypatch):
        tray = self.tray(qapp)
        path = tmp_path / "config.json"
        path.write_text('{"activity_log_enabled": true, "activity_log_paused": true}')
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        tray.menu.aboutToShow.emit()
        assert tray.activity_menu.pause_action.isChecked() and tray.activity_menu.pause_action.isEnabled()


class TestSettingsApplyAtOnce:
    """Pausing the log or switching cloud sharing off in Settings applies without a restart."""

    def test_save_reports_the_fields_the_user_changed(self, qapp, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"_config_version": 6, "activity_log_enabled": True,
                                    "activity_log_share_with_cloud": True}))
        monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: path)
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
        from desktop_app.settings_window import SettingsWindow
        win = SettingsWindow()
        try:
            win._widgets["activity_log_paused"].setChecked(True)
            win._widgets["activity_log_share_with_cloud"].setChecked(False)
            win._on_save()
            assert win.changed_values == {"activity_log_paused": True, "activity_log_share_with_cloud": False}
        finally:
            win.close()

    def run_settings(self, qapp, monkeypatch, changed, *, restart=False):
        import desktop_app.activity_menu as activity_menu
        import desktop_app.settings_window as settings_window
        from PyQt6.QtWidgets import QDialog, QMessageBox
        from desktop_app.app import JarvisSystemTray

        class FakeWindow:
            changed_values = changed

            def exec(self):
                return QDialog.DialogCode.Accepted

        sent, restarts = [], []
        monkeypatch.setattr(settings_window, "SettingsWindow", FakeWindow)
        monkeypatch.setattr(activity_menu, "send_request", lambda app, action: sent.append(action))
        answer = QMessageBox.StandardButton.Yes if restart else QMessageBox.StandardButton.No
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answer)
        tray = JarvisSystemTray.__new__(JarvisSystemTray)
        tray.is_listening = True
        tray.stop_daemon = lambda: restarts.append("stop")
        tray.start_daemon = lambda: restarts.append("start")
        tray.show_settings()
        return sent, restarts

    def test_pause_and_stop_sharing_reach_the_daemon_when_the_restart_is_declined(self, qapp, monkeypatch):
        sent, restarts = self.run_settings(qapp, monkeypatch, {"activity_log_paused": True,
                                                               "activity_log_share_with_cloud": False})
        assert sent == ["pause", "stop_sharing"] and restarts == []

    def test_resume_applies_and_switching_sharing_on_waits_for_the_restart(self, qapp, monkeypatch):
        sent, _ = self.run_settings(qapp, monkeypatch, {"activity_log_paused": False,
                                                        "activity_log_share_with_cloud": True})
        assert sent == ["resume"]

    def test_unrelated_changes_send_nothing(self, qapp, monkeypatch):
        sent, _ = self.run_settings(qapp, monkeypatch, {"tts_rate": 200})
        assert sent == []
