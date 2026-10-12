"""Settings > Calendar & Email: the account list, the sign-in buttons, the add dialogs and how the page fits the
settings window and the restart offer. Qt widgets run offscreen; the account store is a stub, except in the
window test, which uses the real store against a loopback calendar server."""
import json
import threading

import pytest

from jarvis.accounts.manage import Outcome, Row


class StubClient:
    def __init__(self, configured):
        self.configured = configured


class StubStore:
    def __init__(self, *, microsoft=True, google=True, outlook="none", rows=None, outcome=None):
        self.configured = {"microsoft": microsoft, "google": google}
        self.state = outlook
        self._rows = list(rows or [])
        self.outcome = outcome or Outcome(True, "Connected owen@example.test: calendar and email.")
        self.calls = []

    def client(self, key):
        return StubClient(self.configured[key])

    def outlook_state(self):
        return self.state

    def rows(self):
        return list(self._rows)

    def connect_oauth(self, provider, *, calendar, mail, cancel):
        self.calls.append(("oauth", provider, calendar, mail, cancel))
        if self.outcome.ok:
            self._rows.append(Row("calendar", "Microsoft", "microsoft", "Microsoft account", "owen@example.test"))
        return self.outcome

    def add_outlook(self, *, calendar, mail):
        self.calls.append(("outlook", calendar, mail))
        return Outcome(True, "Connected Outlook on this PC: calendar and email.")

    def add_imap(self, username, password, **kwargs):
        self.calls.append(("imap", username, password, kwargs))
        return self.outcome

    def add_ics(self, name, url):
        self.calls.append(("ics", name, url))
        return self.outcome

    def test(self, kind, name):
        self.calls.append(("test", kind, name))
        return Outcome(False, f"{name} could not be read: the sign-in has expired or was removed (sign in again).")

    def remove(self, kind, name):
        self.calls.append(("remove", kind, name))
        self._rows = [r for r in self._rows if (r.kind, r.name) != (kind, name)]
        return Outcome(True, f"Removed {name}.")


def sync(job, done):
    done(job())


@pytest.fixture
def page_with(qapp):
    made = []

    def make(store, **kwargs):
        from desktop_app.accounts_page import AccountsPage
        changed = []
        page = AccountsPage(store, on_changed=lambda: changed.append(True), run_async=kwargs.pop("run_async", sync),
                            **kwargs)
        made.append(page)
        return page, changed

    yield make
    for page in made:
        page.close()


def test_sign_in_buttons_without_an_app_id_are_off_and_say_why(page_with):
    page, _ = page_with(StubStore(microsoft=False, google=True))
    assert not page.microsoft_button.isEnabled()
    assert "not set up in this build" in page.microsoft_button.toolTip()
    assert "not set up in this build" in page.microsoft_hint.text()
    assert page.google_button.isEnabled()


def test_outlook_is_offered_only_when_classic_outlook_is_installed(page_with):
    page, _ = page_with(StubStore(outlook="none"))
    assert page.outlook_button.isHidden()
    page, changed = page_with(StubStore(outlook="classic"))
    assert not page.outlook_button.isHidden()
    page.outlook_button.click()
    assert page._store.calls == [("outlook", True, True)] and changed == [True]


def test_test_and_remove_appear_only_once_something_is_connected(page_with):
    store = StubStore()
    page, _changed = page_with(store)
    assert page.test_button.isHidden() and page.remove_button.isHidden()
    page.microsoft_button.click()
    assert not page.test_button.isHidden() and not page.remove_button.isHidden()


def test_microsoft_sign_in_connects_what_is_ticked_and_refreshes_the_list(page_with):
    store = StubStore()
    page, changed = page_with(store)
    assert page.list.count() == 0 and not page.empty_label.isHidden()
    page.ms_mail.setChecked(False)
    page.microsoft_button.click()
    kind, provider, calendar, mail, cancel = store.calls[0]
    assert (provider, calendar, mail) == ("microsoft", True, False)
    assert isinstance(cancel, threading.Event)
    assert page.list.count() == 1 and "owen@example.test" in page.list.item(0).text()
    assert "Connected" in page.status.text() and changed == [True]


def test_nothing_ticked_starts_no_sign_in(page_with):
    store = StubStore()
    page, _ = page_with(store)
    page.ms_calendar.setChecked(False)
    page.ms_mail.setChecked(False)
    page.microsoft_button.click()
    assert store.calls == [] and "Tick" in page.status.text()


def test_google_sign_in_is_calendar_only(page_with):
    store = StubStore()
    page, _ = page_with(store)
    page.google_button.click()
    assert store.calls[0][1:4] == ("google", True, False)


def test_while_signing_in_the_page_waits_and_cancel_stops_the_sign_in(page_with):
    pending = []
    store = StubStore(outcome=Outcome(False, "Sign-in cancelled."))
    page, changed = page_with(store, run_async=lambda job, done: pending.append((job, done)))
    page.microsoft_button.click()
    assert not page.progress.isHidden() and not page.microsoft_button.isEnabled()
    page.cancel_button.click()
    job, done = pending[0]
    outcome = job()
    assert store.calls[0][4].is_set()  # the cancel reached the sign-in
    done(outcome)
    assert page.progress.isHidden() and page.microsoft_button.isEnabled()
    assert page.status.text() == "Sign-in cancelled." and changed == []


def test_test_reports_an_expired_sign_in_and_remove_asks_first(page_with):
    rows = [Row("email", "Microsoft", "graph", "Microsoft account", "owen@example.test")]
    store = StubStore(rows=rows)
    answers = [False, True]
    page, changed = page_with(store, confirm=lambda parent, name: answers.pop(0))
    page.test_button.click()
    assert "sign in again" in page.status.text()
    page.remove_button.click()
    assert ("remove", "email", "Microsoft") not in store.calls
    page.remove_button.click()
    assert ("remove", "email", "Microsoft") in store.calls and page.list.count() == 0 and changed == [True]


def test_gmail_dialog_opens_the_app_passwords_page_and_saves_only_after_a_passing_test(qapp):
    from desktop_app.accounts_page import APP_PASSWORDS_URL, GmailDialog
    opened = []
    store = StubStore(outcome=Outcome(False, "Could not read the inbox: the server refused the sign-in."))
    dialog = GmailDialog(store, run_async=sync, open_url=opened.append)
    dialog.open_button.click()
    assert opened == [APP_PASSWORDS_URL]
    dialog.address.setText("me@gmail.com")
    dialog.password.setText("abcd efgh ijkl mnop")
    assert dialog.password.echoMode() == dialog.password.EchoMode.Password
    dialog.save_button.click()
    assert dialog.outcome is None and "refused" in dialog.status.text()
    store.outcome = Outcome(True, "Connected me@gmail.com: the inbox can be read.")
    dialog.save_button.click()
    assert dialog.outcome is store.outcome and dialog.result() == dialog.DialogCode.Accepted
    assert store.calls[-1] == ("imap", "me@gmail.com", "abcd efgh ijkl mnop", {"gmail": True})


def test_imap_and_link_dialogs_hand_their_fields_to_the_store(qapp):
    from desktop_app.accounts_page import CalendarLinkDialog, ImapDialog
    store = StubStore()
    imap = ImapDialog(store, run_async=sync)
    imap.host.setText("imap.example.com")
    imap.username.setText("me@example.com")
    imap.password.setText("pw")
    imap.save_button.click()
    assert store.calls[-1] == ("imap", "me@example.com", "pw",
                               {"host": "imap.example.com", "port": 993, "gmail": False})
    link = CalendarLinkDialog(store, run_async=sync)
    link.name.setText("Uni")
    link.link.setText("webcal://example.test/cal.ics")
    link.save_button.click()
    assert store.calls[-1] == ("ics", "Uni", "webcal://example.test/cal.ics")


def test_the_window_lists_the_page_and_follows_an_added_calendar(qapp, tmp_path, monkeypatch):
    from fake_ics_server import FakeIcsServer
    from jarvis import credentials
    from test_account_signin import MemoryStore
    monkeypatch.setattr(credentials, "_backend", MemoryStore())
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_config_version": 6, "db_path": str(tmp_path / "j.db")}), encoding="utf-8")
    monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
    from desktop_app.settings_window import SettingsWindow
    win = SettingsWindow()
    try:
        assert "Calendar & Email" in win.visible_pages()
        assert not win.accounts_changed and not win._widgets["calendar_enabled"].isChecked()
        server = FakeIcsServer("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
        try:
            page = win.accounts_page
            page._finish(page._store.add_ics("Uni", server.url), changed=True)
        finally:
            server.stop()
        assert win.accounts_changed and win._widgets["calendar_enabled"].isChecked()
        assert win.is_field_shown("calendar_share_with_cloud")
        win._on_save()
        saved = json.loads(cfg.read_text(encoding="utf-8"))
        assert saved["calendar_sources"] == [{"name": "Uni", "type": "ics"}] and saved["calendar_enabled"] is True
    finally:
        win.close()


def test_closing_settings_after_adding_an_account_offers_a_restart_even_when_cancelled(qapp, monkeypatch):
    import desktop_app.settings_window as settings_window
    from PyQt6.QtWidgets import QDialog, QMessageBox
    from desktop_app.app import JarvisSystemTray

    class CancelledWindow:
        changed_values = {}
        accounts_changed = True

        def exec(self):
            return QDialog.DialogCode.Rejected

    asked, restarts = [], []
    monkeypatch.setattr(settings_window, "SettingsWindow", CancelledWindow)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.Yes)
    tray = JarvisSystemTray.__new__(JarvisSystemTray)
    tray.is_listening = True
    tray.stop_daemon = lambda: restarts.append("stop")
    tray.start_daemon = lambda: restarts.append("start")
    tray.show_settings()
    assert asked and restarts == ["stop", "start"]


def test_results_from_worker_threads_always_reach_the_page(qapp):
    import gc
    import time
    from desktop_app.accounts_page import run_in_thread
    delivered = []
    on_gui_thread = []
    for number in range(20):
        run_in_thread(lambda n=number: (time.sleep(0.01), n)[1],
                      lambda n: (delivered.append(n), on_gui_thread.append(threading.current_thread()
                                                                           is threading.main_thread())))
    deadline = time.monotonic() + 10
    while len(delivered) < 20 and time.monotonic() < deadline:
        gc.collect()
        qapp.processEvents()
        time.sleep(0.01)
    assert sorted(delivered) == list(range(20)) and all(on_gui_thread)


def test_closing_settings_cancels_a_waiting_sign_in_and_ignores_its_late_result(page_with):
    pending = []
    store = StubStore(outcome=Outcome(False, "Sign-in cancelled."))
    page, changed = page_with(store, run_async=lambda job, done: pending.append((job, done)))
    page.microsoft_button.click()
    page.shutdown()
    job, done = pending[0]
    outcome = job()
    assert store.calls[0][4].is_set()
    done(Outcome(True, "Connected."))  # arrives after the window closed
    assert changed == [] and page.status.text() != "Connected."
