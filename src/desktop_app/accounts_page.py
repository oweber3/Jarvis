"""Settings > Calendar & Email: the connected calendars and mailboxes, and the ways to add one without a
terminal (sign in with Microsoft or Google, Gmail with an app password, another IMAP mailbox, a calendar
link, classic Outlook on this PC).

The page only arranges widgets: every add, test and remove is ``jarvis.accounts.manage.AccountStore``,
run off the GUI thread. Changes are written at once (secrets to Windows Credential Manager) and take
effect when Jarvis restarts. See ``settings_window.spec.md`` (Calendar & Email).
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from desktop_app.themes import apply_theme, set_role
from jarvis.debug import debug_log

APP_PASSWORDS_URL = "https://myaccount.google.com/apppasswords"
RESTART_NOTE = ("Changes take effect after Jarvis restarts; Jarvis offers to restart when you close Settings. "
                "Read only: Jarvis never sends, deletes or changes anything. Sign-ins and passwords stay in "
                "Windows Credential Manager on this PC. To withdraw access later, remove the account here, and "
                "for a sign-in also at account.microsoft.com or myaccount.google.com/permissions.")
GMAIL_STEPS = ("Gmail needs an app password: a separate 16-letter password just for Jarvis.\n"
               "1. In your Google Account, turn on 2-Step Verification (Security).\n"
               "2. Open the App passwords page, name it Jarvis and press Create.\n"
               "3. Copy the 16 letters it shows into the box below.\n"
               "You can delete it there at any time without changing your Google password.")
LINK_HELP = ("Paste a calendar's private iCal address (it ends in .ics or starts with webcal://).\n"
             "Google Calendar: Settings > your calendar > Integrate calendar > Secret address in iCal format.\n"
             "Outlook.com: Settings > Calendar > Shared calendars > Publish a calendar > ICS link.\n"
             "The link works like a password: anyone with it can read that calendar.")


class _Relay(QObject):
    """Lives on the GUI thread, so a result emitted from a worker is delivered there (queued)."""

    finished = pyqtSignal(object, object)

    def __init__(self):
        super().__init__()
        self.finished.connect(self.deliver)

    @pyqtSlot(object, object)
    def deliver(self, callback, result) -> None:
        _relays.discard(self)
        callback(result)


# Relays waiting for their result. Holding them here keeps each one alive until its queued signal has been
# delivered; the worker thread alone would not (it may be gone before the GUI thread runs the slot).
_relays: set = set()


def run_in_thread(job: Callable[[], Any], done: Callable[[Any], None]) -> None:
    """Run ``job`` on a worker thread and hand its result to ``done`` on the GUI thread."""
    relay = _Relay()
    _relays.add(relay)

    def work() -> None:
        try:
            result = job()
        except Exception as exc:  # noqa: BLE001 - a crash must still end the busy state
            debug_log(f"account job failed ({type(exc).__name__})", "settings")
            from jarvis.accounts.manage import Outcome
            result = Outcome(False, "Something went wrong. Try again.")
        relay.finished.emit(done, result)

    threading.Thread(target=work, daemon=True, name="settings-accounts").start()


def _open_url(url: str) -> bool:
    import webbrowser
    return bool(webbrowser.open(url))


def _confirm_remove(parent: QWidget, name: str) -> bool:
    answer = QMessageBox.question(parent, "Remove account",
                                  f"Remove {name}?\n\nIts stored password or sign-in is deleted from this PC. You can "
                                  "add it again later.",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


class AccountsPage(QWidget):
    """The account list with Test and Remove, and the add options."""

    def __init__(self, store, *, on_changed: Callable[[], None] = lambda: None,
                 run_async: Callable = run_in_thread, open_url: Callable[[str], bool] = _open_url,
                 confirm: Callable[[QWidget, str], bool] = _confirm_remove, parent=None):
        super().__init__(parent)
        self._store = store
        self._confirm = confirm
        self._on_changed = on_changed
        self._run = run_async
        self._open_url = open_url
        self._cancel: Optional[threading.Event] = None
        self._closed = False
        self._busy_buttons: list = []
        self._build()
        self.refresh()

    # -- Layout --------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        # Never taller than its contents, so a short page does not spread its rows apart.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        title = QLabel("Connected accounts")
        title.setObjectName("section_title")
        layout.addWidget(title)
        self.list = QListWidget()
        self.list.setMinimumHeight(110)
        self.list.setMaximumHeight(200)
        layout.addWidget(self.list)
        self.empty_label = _hint("Nothing is connected yet. Add a calendar or a mailbox below.")
        layout.addWidget(self.empty_label)
        row = QHBoxLayout()
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip("Read the selected calendar or inbox now to check it works.")
        self.test_button.clicked.connect(self._test)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("danger")
        self.remove_button.setToolTip("Remove the selected account and its stored password or sign-in from this PC.")
        self.remove_button.clicked.connect(self._remove)
        row.addWidget(self.test_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.list.currentRowChanged.connect(lambda _row: self._update_buttons())

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("status-muted")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        progress = QHBoxLayout()
        self.progress_label = QLabel("Finish signing in in your browser. Waiting up to 3 minutes...")
        self.progress_label.setObjectName("body")
        self.cancel_button = QPushButton("Cancel sign-in")
        self.cancel_button.clicked.connect(self._cancel_sign_in)
        progress.addWidget(self.progress_label, 1)
        progress.addWidget(self.cancel_button)
        self.progress = QWidget()
        self.progress.setLayout(progress)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        add_title = QLabel("Add an account")
        add_title.setObjectName("section_title")
        layout.addWidget(add_title)

        microsoft = QHBoxLayout()
        self.ms_calendar = QCheckBox("Calendar")
        self.ms_mail = QCheckBox("Email")
        for box in (self.ms_calendar, self.ms_mail):
            box.setChecked(True)
        self.microsoft_button = QPushButton("Sign in with Microsoft")
        self.microsoft_button.setObjectName("primary")
        self.microsoft_button.clicked.connect(lambda: self._sign_in("microsoft"))
        microsoft.addWidget(self.microsoft_button)
        microsoft.addWidget(self.ms_calendar)
        microsoft.addWidget(self.ms_mail)
        microsoft.addStretch(1)
        layout.addLayout(microsoft)
        self.microsoft_hint = _hint("Outlook.com, Hotmail, Live and Microsoft 365 work or school accounts. "
                                    "Your browser opens Microsoft's own sign-in page; Jarvis never sees your password.")
        layout.addWidget(self.microsoft_hint)

        self.google_button = QPushButton("Sign in with Google")
        self.google_button.setObjectName("primary")
        self.google_button.clicked.connect(lambda: self._sign_in("google"))
        google = QHBoxLayout()
        google.addWidget(self.google_button)
        google.addStretch(1)
        layout.addLayout(google)
        self.google_hint = _hint("Google Calendar, every calendar you show there. For Gmail, use Add Gmail below.")
        layout.addWidget(self.google_hint)

        self.outlook_button = QPushButton("Use Outlook on this PC")
        self.outlook_button.setToolTip("Read the calendar and inbox of classic Outlook, which is installed here.")
        self.outlook_button.clicked.connect(self._outlook)
        self.outlook_hint = _hint("Classic Outlook is installed here: one click connects its calendar and inbox, "
                                  "with no network needed.")
        outlook = QHBoxLayout()
        outlook.addWidget(self.outlook_button)
        outlook.addStretch(1)
        layout.addLayout(outlook)
        layout.addWidget(self.outlook_hint)

        more = QHBoxLayout()
        self.gmail_button = QPushButton("Add Gmail...")
        self.gmail_button.clicked.connect(lambda: self._dialog(GmailDialog))
        self.imap_button = QPushButton("Other email (IMAP)...")
        self.imap_button.clicked.connect(lambda: self._dialog(ImapDialog))
        self.link_button = QPushButton("Paste a calendar link...")
        self.link_button.clicked.connect(lambda: self._dialog(CalendarLinkDialog))
        for button in (self.gmail_button, self.imap_button, self.link_button):
            more.addWidget(button)
        more.addStretch(1)
        layout.addLayout(more)
        layout.addWidget(_hint(RESTART_NOTE))

        self._busy_buttons = [self.test_button, self.remove_button, self.microsoft_button, self.google_button,
                              self.outlook_button, self.gmail_button, self.imap_button, self.link_button]
        # Sign-in needs an app ID registered with the provider; without one the option is not shown at all.
        for key, widgets in (("microsoft", (self.microsoft_button, self.ms_calendar, self.ms_mail,
                                            self.microsoft_hint)),
                             ("google", (self.google_button, self.google_hint))):
            if not self._store.client(key).configured:
                for widget in widgets:
                    widget.setVisible(False)
        classic = self._store.outlook_state() == "classic"
        self.outlook_button.setVisible(classic)
        self.outlook_hint.setVisible(classic)

    # -- State ---------------------------------------------------------------

    def refresh(self) -> None:
        self.list.clear()
        for row in self._store.rows():
            what = "Calendar" if row.kind == "calendar" else "Email"
            text = f"{what}: {row.name}  ({row.label}{', ' + row.detail if row.detail else ''})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, (row.kind, row.name))
            self.list.addItem(item)
        self.empty_label.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)
        self.test_button.setVisible(self.list.count() > 0)
        self.remove_button.setVisible(self.list.count() > 0)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._update_buttons()

    def _update_buttons(self, busy: bool = False) -> None:
        for button in self._busy_buttons:
            button.setEnabled(not busy)
        if not busy:
            selected = self.list.currentItem() is not None
            self.test_button.setEnabled(selected)
            self.remove_button.setEnabled(selected)
            self.microsoft_button.setEnabled(self._store.client("microsoft").configured)
            self.ms_calendar.setEnabled(self.microsoft_button.isEnabled())
            self.ms_mail.setEnabled(self.microsoft_button.isEnabled())
            self.google_button.setEnabled(self._store.client("google").configured)

    def _show(self, outcome) -> None:
        self.status.setText(outcome.message)
        set_role(self.status, "status-success" if outcome.ok else "status-error")

    def _start(self, message: str) -> None:
        self.status.setText(message)
        set_role(self.status, "status-muted")
        self._update_buttons(busy=True)

    def shutdown(self) -> None:
        """The window is closing: stop a sign-in in progress, so its loopback receiver closes now, and ignore
        results that arrive after the widgets are gone."""
        self._closed = True
        if self._cancel is not None:
            self._cancel.set()

    def _finish(self, outcome, *, changed: bool) -> None:
        if self._closed:
            return
        self._cancel = None
        self.progress.setVisible(False)
        self._update_buttons()
        self._show(outcome)
        if changed and outcome.ok:
            self.refresh()
            self._on_changed()

    def _selected(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    # -- Actions -------------------------------------------------------------

    def _sign_in(self, provider_key: str) -> None:
        calendar = self.ms_calendar.isChecked() if provider_key == "microsoft" else True
        mail = self.ms_mail.isChecked() if provider_key == "microsoft" else False
        if not calendar and not mail:
            self.status.setText("Tick Calendar, Email or both first.")
            set_role(self.status, "status-warning")
            return
        self._cancel = threading.Event()
        cancel = self._cancel
        self._start("Opening your browser...")
        self.progress.setVisible(True)
        self.cancel_button.setEnabled(True)
        self._run(lambda: self._store.connect_oauth(provider_key, calendar=calendar, mail=mail, cancel=cancel),
                  lambda outcome: self._finish(outcome, changed=True))

    def _cancel_sign_in(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.cancel_button.setEnabled(False)

    def _outlook(self) -> None:
        self._start("Reading Outlook on this PC...")
        self._run(lambda: self._store.add_outlook(calendar=True, mail=True),
                  lambda outcome: self._finish(outcome, changed=True))

    def _test(self) -> None:
        selected = self._selected()
        if selected:
            self._start(f"Testing {selected[1]}...")
            self._run(lambda: self._store.test(*selected), lambda outcome: self._finish(outcome, changed=False))

    def _remove(self) -> None:
        selected = self._selected()
        if selected and self._confirm(self, selected[1]):
            self._finish(self._store.remove(*selected), changed=True)

    def _dialog(self, dialog_class) -> None:
        dialog = dialog_class(self._store, run_async=self._run, open_url=self._open_url, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.outcome is not None:
            self._finish(dialog.outcome, changed=True)


class _AddDialog(QDialog):
    """A small form that tests the account and saves it only when the test passes."""

    title = ""
    intro = ""

    def __init__(self, store, *, run_async: Callable = run_in_thread, open_url: Callable[[str], bool] = _open_url,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.title)
        self.setMinimumWidth(480)
        self._store = store
        self._run = run_async
        self._open_url = open_url
        self.outcome = None
        layout = QVBoxLayout(self)
        if self.intro:
            layout.addWidget(_hint(self.intro))
        self.extra(layout)
        self.form = QFormLayout()
        layout.addLayout(self.form)
        self.fields()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("status-muted")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Test and save")
        self.save_button.setObjectName("primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        apply_theme(self)

    def extra(self, layout: QVBoxLayout) -> None:
        pass

    def fields(self) -> None:
        raise NotImplementedError

    def job(self) -> Callable[[], Any]:
        raise NotImplementedError

    def _save(self) -> None:
        self.save_button.setEnabled(False)
        self.status.setText("Testing...")
        set_role(self.status, "status-muted")
        self._run(self.job(), self._done)

    def _done(self, outcome) -> None:
        self.save_button.setEnabled(True)
        if outcome.ok:
            self.outcome = outcome
            self.accept()
            return
        self.status.setText(outcome.message)
        set_role(self.status, "status-error")


def _password_field(placeholder: str = "") -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setPlaceholderText(placeholder)
    return field


class GmailDialog(_AddDialog):
    title = "Add Gmail"
    intro = GMAIL_STEPS

    def extra(self, layout: QVBoxLayout) -> None:
        self.open_button = QPushButton("Open the App passwords page")
        self.open_button.clicked.connect(lambda: self._open_url(APP_PASSWORDS_URL))
        row = QHBoxLayout()
        row.addWidget(self.open_button)
        row.addStretch(1)
        layout.addLayout(row)

    def fields(self) -> None:
        self.address = QLineEdit()
        self.address.setPlaceholderText("you@gmail.com")
        self.password = _password_field("16 letters")
        self.form.addRow("Gmail address", self.address)
        self.form.addRow("App password", self.password)

    def job(self):
        address, password = self.address.text(), self.password.text()
        return lambda: self._store.add_imap(address, password, gmail=True)


class ImapDialog(_AddDialog):
    title = "Add another email account (IMAP)"
    intro = ("Any mailbox that offers IMAP over TLS. Your provider's help pages list its IMAP server; the port is "
             "almost always 993. Some providers need an app password instead of your normal password.")

    def fields(self) -> None:
        self.host = QLineEdit()
        self.host.setPlaceholderText("imap.example.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(993)
        self.username = QLineEdit()
        self.username.setPlaceholderText("you@example.com")
        self.password = _password_field()
        self.form.addRow("IMAP server", self.host)
        self.form.addRow("Port", self.port)
        self.form.addRow("Email address", self.username)
        self.form.addRow("Password", self.password)

    def job(self):
        host, port, username, password = self.host.text(), self.port.value(), self.username.text(), self.password.text()
        return lambda: self._store.add_imap(username, password, host=host, port=port, gmail=False)


class CalendarLinkDialog(_AddDialog):
    title = "Paste a calendar link"
    intro = LINK_HELP

    def fields(self) -> None:
        self.name = QLineEdit("Calendar")
        self.link = _password_field("https://... .ics or webcal://...")
        self.form.addRow("Name", self.name)
        self.form.addRow("Calendar link", self.link)

    def job(self):
        name, link = self.name.text(), self.link.text()
        return lambda: self._store.add_ics(name, link)
