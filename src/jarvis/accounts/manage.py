"""Connect, test and remove calendars and mailboxes: the operations behind Settings > Calendar & Email,
also used by the setup commands.

Every add tests the account before anything is kept: secrets go to the credential store first and are
removed again when the test fails, and the config entry (with ``calendar_enabled`` or ``email_enabled``
turned on) is written only after the test passes. Outcomes carry plain-language messages for the user;
logs carry kinds and reason codes only. Nothing here knows about Qt. See ``accounts.spec.md``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import credentials
from ..debug import debug_log
from . import logins, oauth

CALENDAR_ERRORS = {
    "no_address": "no address is stored for it",
    "unreachable": "it did not answer (offline, or a mistyped address)",
    "http_error": "the address was refused (it may have been reset; copy the current secret address)",
    "not_a_calendar": "the address did not return a calendar (use the secret address in iCal format)",
    "too_large": "the calendar file is too large",
    "not_signed_in": "the sign-in has expired or was removed (sign in again)",
    "server_error": "the calendar service returned an error",
    "outlook_not_installed": "classic Outlook is not installed",
    "outlook_new_only": "only the new Outlook is installed, and it has no interface Jarvis can read",
    "outlook_timeout": "Outlook did not answer in time",
    "outlook_failed": "Outlook could not be read",
}
MAIL_ERRORS = {
    "no_password": "no password is stored for it",
    "auth_failed": "the server refused the sign-in (Gmail needs an app password, not your normal password)",
    "unreachable": "the server did not answer (offline, or a mistyped server name)",
    "server_error": "the server returned an error",
    "not_signed_in": "the Microsoft sign-in has expired or was removed (sign in again)",
    "outlook_not_installed": "classic Outlook is not installed",
    "outlook_new_only": "only the new Outlook is installed, and it has no interface Jarvis can read",
    "outlook_timeout": "Outlook did not answer in time",
    "outlook_failed": "Outlook could not be read",
}
SIGN_IN_ERRORS = {
    "not_configured": "Sign-in with {provider} is not set up in this build of Jarvis.",
    "browser": "The web browser could not be opened.",
    "cancelled": "Sign-in cancelled.",
    "timeout": "The sign-in was not finished within 3 minutes. Try again.",
    "state_mismatch": "The browser's reply did not belong to this sign-in, so it was ignored. Try again.",
    "denied": "Permission was not given, so nothing was connected.",
    "provider_error": "{provider} reported a problem with the sign-in. Try again.",
    "no_code": "{provider} did not finish the sign-in. Try again.",
    "unreachable": "{provider} could not be reached. Check the internet connection.",
    "token_refused": "{provider} did not accept the sign-in. Try again.",
    "scope_missing": "Jarvis did not get permission to read what you ticked. Sign in again and leave the "
                     "permission ticked on the consent page.",
}
_CALENDAR_LABELS = {"microsoft": "Microsoft account", "google": "Google account", "ics": "Calendar link",
                    "outlook": "Outlook on this PC"}
_MAIL_LABELS = {"graph": "Microsoft account", "imap": "Email (IMAP)", "outlook": "Outlook on this PC"}
# The config entry types each provider's sign-in creates: (calendar type, mail type).
_OAUTH_TYPES = {"microsoft": ("microsoft", "graph"), "google": ("google", None)}
_DEFAULT_NAMES = {"microsoft": "Microsoft", "google": "Google"}
TEST_DAYS = 7


@dataclass
class Row:
    kind: str  # "calendar" or "email"
    name: str
    type: str
    label: str
    detail: str = ""


@dataclass
class Outcome:
    ok: bool
    message: str
    added: List[str] = field(default_factory=list)


def _ok(message: str, added: Optional[List[str]] = None) -> Outcome:
    return Outcome(True, message, added or [])


def _no(message: str) -> Outcome:
    return Outcome(False, message)


class AccountStore:
    """Calendars and mailboxes in one config file and the credential store.

    The arguments after ``config_path`` are seams: the HTTP transport for sign-ins and online reads, how
    the browser is opened, the IMAP connection, which Outlook is installed and the classic Outlook
    runner."""

    def __init__(self, config_path: Optional[Path] = None, *, transport: Any = None,
                 open_browser: Optional[Callable[[str], bool]] = None, imap_connect: Optional[Callable] = None,
                 outlook_state: Optional[Callable[[], str]] = None, outlook_runner: Optional[Callable] = None,
                 sign_in_timeout: float = oauth.SIGN_IN_TIMEOUT_SEC):
        from ..config import _config_path
        self.path = Path(config_path) if config_path else _config_path()
        self._transport = transport
        self._open_browser = open_browser
        self._imap_connect = imap_connect
        self._outlook_state = outlook_state
        self._outlook_runner = outlook_runner
        self._sign_in_timeout = sign_in_timeout

    # ── Config ──────────────────────────────────────────────────────────────

    def _merged(self) -> Dict[str, Any]:
        from ..config import _load_json, get_default_config
        return {**get_default_config(), **_load_json(self.path)}

    def entries(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """The calendar sources and email accounts as Jarvis will read them."""
        from ..config import _CALENDAR_SOURCE_FIELDS, _EMAIL_ACCOUNT_FIELDS, _named_entries
        merged = self._merged()
        return (_named_entries(merged.get("calendar_sources"), _CALENDAR_SOURCE_FIELDS),
                _named_entries(merged.get("email_accounts"), _EMAIL_ACCOUNT_FIELDS))

    def _save(self, calendars: List[Dict[str, Any]], mail: List[Dict[str, Any]], *,
              calendar_on: Optional[bool] = None, email_on: Optional[bool] = None) -> bool:
        from ..config import update_config_values
        updates: Dict[str, Any] = {"calendar_sources": calendars, "email_accounts": mail}
        if calendar_on is not None:
            updates["calendar_enabled"] = calendar_on
        if email_on is not None:
            updates["email_enabled"] = email_on
        return update_config_values(updates, self.path)

    def client(self, provider_key: str) -> oauth.Client:
        return oauth.client_for(provider_key, self._merged())

    def outlook_state(self) -> str:
        """``classic`` when classic Outlook can be read on this PC, otherwise ``new_only`` or ``none``."""
        if self._outlook_state is not None:
            return self._outlook_state()
        from ..platform.windows.outlook import install_state
        return install_state()

    def rows(self) -> List[Row]:
        calendars, mail = self.entries()
        rows = []
        for entry in calendars:
            detail = entry.get("user", "") if entry["type"] in ("microsoft", "google") else ""
            rows.append(Row("calendar", entry["name"], entry["type"], _CALENDAR_LABELS[entry["type"]], detail))
        for entry in mail:
            detail = {"graph": entry.get("user", ""), "imap": entry.get("username", "")}.get(entry["type"], "")
            rows.append(Row("email", entry["name"], entry["type"], _MAIL_LABELS[entry["type"]], detail))
        return rows

    # ── Building and testing ────────────────────────────────────────────────

    def _cfg(self) -> Any:
        merged = self._merged()
        return SimpleNamespace(db_path=merged.get("db_path"), calendar_cache_minutes=0)

    def _source(self, entry: Dict[str, Any], session: Any = None):
        from ..agenda import service
        from ..agenda.online import GoogleCalendarSource, MicrosoftCalendarSource
        from ..agenda.sources import IcsSource, OutlookCalendarSource
        kind, name = entry["type"], entry["name"]
        if kind in ("microsoft", "google"):
            session = session or logins.session_for(entry.get("login") or "")
            source_class = MicrosoftCalendarSource if kind == "microsoft" else GoogleCalendarSource
            return source_class(name, session=session, transport=self._transport)
        if kind == "ics":
            key = credentials.account_key("calendar_url", name)
            return IcsSource(name, cache_dir=service.cache_dir(self._cfg()), ttl_sec=0,
                             address=lambda: credentials.get_secret(key))
        return OutlookCalendarSource(name, runner=self._outlook_runner, install_state=self._outlook_state)

    def _account(self, entry: Dict[str, Any], session: Any = None):
        from ..mail.graph import GraphAccount
        from ..mail.imap import ImapAccount
        from ..mail.outlook import OutlookMailAccount
        kind, name = entry["type"], entry["name"]
        if kind == "graph":
            return GraphAccount(name, session=session or logins.session_for(entry.get("login") or ""),
                                transport=self._transport)
        if kind == "imap":
            key = credentials.account_key("email_password", name)
            return ImapAccount(name, host=entry.get("host") or "imap.gmail.com", port=entry.get("port") or 993,
                               username=entry.get("username") or "", password=lambda: credentials.get_secret(key),
                               connect=self._imap_connect)
        return OutlookMailAccount(name, runner=self._outlook_runner, install_state=self._outlook_state)

    def _check_calendar(self, entry: Dict[str, Any], session: Any = None) -> Tuple[bool, str]:
        now = datetime.now().astimezone()
        result = self._source(entry, session).read(now, now + timedelta(days=TEST_DAYS))
        if result.ok and not result.offline_copy:
            count = len(result.occurrences)
            return True, f"{count} event{'s' if count != 1 else ''} in the next {TEST_DAYS} days"
        return False, CALENDAR_ERRORS.get(result.error, "it could not be read")

    def _check_mail(self, entry: Dict[str, Any], session: Any = None) -> Tuple[bool, str]:
        result = self._account(entry, session).test()
        if result.ok:
            return True, "the inbox can be read"
        return False, MAIL_ERRORS.get(result.error, "it could not be read")

    def test(self, kind: str, name: str) -> Outcome:
        calendars, mail = self.entries()
        entry = next((e for e in (calendars if kind == "calendar" else mail) if e["name"] == name), None)
        if entry is None:
            return _no(f"There is no {kind} called {name}.")
        ok, detail = (self._check_calendar if kind == "calendar" else self._check_mail)(entry)
        debug_log(f"account test: {kind} {entry['type']} {'ok' if ok else 'failed'}", "accounts")
        return Outcome(ok, f"{name}: {detail}." if ok else f"{name} could not be read: {detail}.")

    # ── Names ───────────────────────────────────────────────────────────────

    @staticmethod
    def _free_name(base: str, taken: List[Dict[str, Any]]) -> str:
        names = {e["name"].casefold() for e in taken}
        if base.casefold() not in names:
            return base
        number = 2
        while f"{base} {number}".casefold() in names:
            number += 1
        return f"{base} {number}"

    # ── Browser sign-in ─────────────────────────────────────────────────────

    def connect_oauth(self, provider_key: str, *, calendar: bool = True, mail: bool = True,
                      cancel: Optional[threading.Event] = None) -> Outcome:
        """Sign in with Microsoft or Google in the browser and connect what was ticked.

        Signing in again to an account that is already connected replaces its sign-in and connects
        exactly what is ticked now, keeping the existing names."""
        provider = oauth.PROVIDERS[provider_key]
        calendar_type, mail_type = _OAUTH_TYPES[provider_key]
        mail = bool(mail and mail_type)
        if not calendar and not mail:
            return _no("Tick Calendar, Email or both first.")
        client = self.client(provider_key)
        words = {"provider": provider.label}
        scopes = oauth.scopes_for(provider, calendar=calendar, mail=mail)
        try:
            tokens = oauth.authorize(provider, client, scopes, open_browser=self._open_browser, cancel=cancel,
                                     timeout=self._sign_in_timeout, transport=self._transport)
        except oauth.OAuthError as exc:
            return _no(SIGN_IN_ERRORS.get(exc.code, "The sign-in did not finish.").format(**words))

        pending = logins.LoginSession("pending", transport=self._transport)
        pending.prime(tokens)
        user = self._identify(provider_key, pending)
        login_id = logins.new_login_id(provider_key)
        if not logins.save(login_id, logins.LoginRecord.from_tokens(provider_key, client, tokens, scopes)):
            return _no("Windows Credential Manager could not store the sign-in. Nothing was saved.")
        logins.session_for(login_id).prime(tokens)
        outcome = None
        try:
            outcome = self._connect_entries(provider_key, login_id, user, calendar, mail)
            return outcome
        finally:
            if outcome is None or not outcome.ok:  # a failed test, a write error or anything unexpected
                logins.delete(login_id)

    def _connect_entries(self, provider_key: str, login_id: str, user: str, calendar: bool, mail: bool) -> Outcome:
        """Test the new sign-in's calendar and mailbox and write them in place of the same account's old ones."""
        calendar_type, mail_type = _OAUTH_TYPES[provider_key]
        session = logins.session_for(login_id)

        calendars, mailboxes = self.entries()
        same = (lambda e: user and e.get("user", "").casefold() == user.casefold())
        old_calendar = next((e for e in calendars if e["type"] == calendar_type and same(e)), None)
        old_mail = next((e for e in mailboxes if e["type"] == mail_type and same(e)), None) if mail_type else None
        kept_calendars = [e for e in calendars if e is not old_calendar]
        kept_mail = [e for e in mailboxes if e is not old_mail]
        base = _DEFAULT_NAMES[provider_key]
        new_entries: Dict[str, Dict[str, Any]] = {}
        if calendar:
            new_entries["calendar"] = {"name": old_calendar["name"] if old_calendar else
                                       self._free_name(base, kept_calendars),
                                       "type": calendar_type, "login": login_id, "user": user}
        if mail:
            new_entries["email"] = {"name": old_mail["name"] if old_mail else self._free_name(base, kept_mail),
                                    "type": mail_type, "login": login_id, "user": user}
        for kind, entry in new_entries.items():
            ok, detail = (self._check_calendar if kind == "calendar" else self._check_mail)(entry, session)
            if not ok:
                debug_log(f"{provider_key} sign-in kept nothing: {kind} test failed", "accounts")
                return _no(f"Signed in, but the {kind} could not be read: {detail}. Nothing was saved.")

        kept_calendars += [new_entries["calendar"]] if calendar else []
        kept_mail += [new_entries["email"]] if mail else []
        if not self._save(kept_calendars, kept_mail,
                          calendar_on=True if calendar else (False if not kept_calendars else None),
                          email_on=True if mail else (False if not kept_mail else None)):
            return _no("The settings file could not be written. Nothing was saved.")
        self._forget_unused_logins(kept_calendars, kept_mail, [e for e in (old_calendar, old_mail) if e])
        what = " and ".join(k for k in ("calendar", "email") if k in new_entries)
        debug_log(f"{provider_key} sign-in connected {what}", "accounts")
        who = f" {user}" if user else ""
        return _ok(f"Connected{who}: {what}.", [f"{k}:{e['name']}" for k, e in new_entries.items()])

    def _identify(self, provider_key: str, session: Any) -> str:
        from .http import ApiError
        try:
            if provider_key == "microsoft":
                from ..mail.graph import microsoft_user
                return microsoft_user(session, transport=self._transport)
            from ..agenda.online import google_primary_calendar
            return google_primary_calendar(session, transport=self._transport)
        except ApiError as exc:
            debug_log(f"{provider_key} account address not read ({exc.code})", "accounts")
            return ""

    @staticmethod
    def _forget_unused_logins(calendars: List[Dict[str, Any]], mail: List[Dict[str, Any]],
                              removed: List[Dict[str, Any]]) -> None:
        in_use = {e.get("login") for e in calendars + mail if e.get("login")}
        for login_id in {e.get("login") for e in removed if e.get("login")} - in_use:
            logins.delete(login_id)

    # ── Password, link and Outlook kinds ────────────────────────────────────

    def add_imap(self, username: str, password: str, *, host: str = "imap.gmail.com", port: int = 993,
                 gmail: bool = True) -> Outcome:
        """Gmail with an app password, or any IMAP mailbox. Adding the same mailbox again updates its password."""
        username, host = (username or "").strip(), (host or "").strip()
        password = (password or "").replace(" ", "").strip() if gmail else (password or "").strip()
        if not username or (gmail and "@" not in username):
            return _no("Enter the full email address.")
        if not password:
            return _no("Enter the app password." if gmail else "Enter the password.")
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = 0
        if not host or not 1 <= port <= 65535:
            return _no("Enter the mail server and port.")
        calendars, mail = self.entries()
        same = next((e for e in mail if e["type"] == "imap" and e.get("host", "").casefold() == host.casefold()
                     and e.get("username", "").casefold() == username.casefold()), None)
        others = [e for e in mail if e is not same]
        name = same["name"] if same else self._free_name("Gmail" if gmail else "Email", others)
        entry = {"name": name, "type": "imap", "host": host, "port": int(port), "username": username}
        key = credentials.account_key("email_password", name)
        previous = credentials.get_secret(key) if same else ""
        if not credentials.set_secret(key, password):
            return _no("Windows Credential Manager could not store the password. Nothing was saved.")
        ok, detail = self._check_mail(entry)
        if not ok:
            if previous:
                credentials.set_secret(key, previous)
            else:
                credentials.delete_secret(key)
            return _no(f"Could not read the inbox: {detail}. Nothing was saved.")
        if not self._save(calendars, others + [entry], email_on=True):
            return _no("The settings file could not be written.")
        debug_log(f"imap mailbox {'updated' if same else 'added'}", "accounts")
        return _ok(f"Connected {username}: {detail}.", [f"email:{name}"])

    def add_ics(self, name: str, url: str) -> Outcome:
        """A calendar's secret ICS or webcal link, tested before it is kept."""
        from ..agenda.sources import normalise_address
        name = (name or "").strip() or "Calendar"
        calendars, mail = self.entries()
        if "#" in name:
            return _no("A name cannot contain #.")
        if any(e["name"].casefold() == name.casefold() for e in calendars):
            return _no(f"A calendar called {name} already exists. Pick another name.")
        try:
            url = normalise_address(url)
        except ValueError as exc:
            return _no(f"That is not a calendar link: {exc}.")
        key = credentials.account_key("calendar_url", name)
        if not credentials.set_secret(key, url):
            return _no("Windows Credential Manager could not store the link. Nothing was saved.")
        entry = {"name": name, "type": "ics"}
        ok, detail = self._check_calendar(entry)
        if not ok:
            credentials.delete_secret(key)
            self._source(entry).forget_copy()
            return _no(f"Could not read the calendar: {detail}. Nothing was saved.")
        if not self._save(calendars + [entry], mail, calendar_on=True):
            return _no("The settings file could not be written.")
        debug_log("calendar link added", "accounts")
        return _ok(f"Connected {name}: {detail}.", [f"calendar:{name}"])

    def add_outlook(self, *, calendar: bool = True, mail: bool = True) -> Outcome:
        """Classic Outlook's default calendar and inbox on this PC."""
        state = self.outlook_state()
        if state != "classic":
            return _no(CALENDAR_ERRORS["outlook_new_only" if state == "new_only" else "outlook_not_installed"]
                       .capitalize() + ".")
        calendars, mailboxes = self.entries()
        added: Dict[str, Dict[str, Any]] = {}
        if calendar and not any(e["type"] == "outlook" for e in calendars):
            added["calendar"] = {"name": self._free_name("Outlook", calendars), "type": "outlook"}
        if mail and not any(e["type"] == "outlook" for e in mailboxes):
            added["email"] = {"name": self._free_name("Outlook", mailboxes), "type": "outlook"}
        if not added:
            return _no("Outlook on this PC is already connected.")
        for kind, entry in added.items():
            ok, detail = (self._check_calendar if kind == "calendar" else self._check_mail)(entry)
            if not ok:
                return _no(f"Could not read Outlook's {kind}: {detail}. Nothing was saved.")
        if not self._save(calendars + ([added["calendar"]] if "calendar" in added else []),
                          mailboxes + ([added["email"]] if "email" in added else []),
                          calendar_on=True if "calendar" in added else None,
                          email_on=True if "email" in added else None):
            return _no("The settings file could not be written.")
        what = " and ".join(added)
        debug_log(f"outlook connected ({what})", "accounts")
        return _ok(f"Connected Outlook on this PC: {what}.", [f"{k}:{e['name']}" for k, e in added.items()])

    # ── Removing ────────────────────────────────────────────────────────────

    def remove(self, kind: str, name: str) -> Outcome:
        """Remove a calendar or mailbox with its stored secret (and its sign-in when nothing else uses it)."""
        calendars, mail = self.entries()
        group = calendars if kind == "calendar" else mail
        entry = next((e for e in group if e["name"] == name), None)
        if entry is None:
            return _no(f"There is no {kind} called {name}.")
        kept = [e for e in group if e is not entry]
        calendars, mail = (kept, mail) if kind == "calendar" else (calendars, kept)
        off = False if not kept else None
        if not self._save(calendars, mail, **({"calendar_on": off} if kind == "calendar" else {"email_on": off})):
            return _no("The settings file could not be written.")
        if entry["type"] == "ics":
            credentials.delete_secret(credentials.account_key("calendar_url", name))
            self._source(entry).forget_copy()
        elif entry["type"] == "imap":
            credentials.delete_secret(credentials.account_key("email_password", name))
        self._forget_unused_logins(calendars, mail, [entry])
        debug_log(f"{kind} {entry['type']} removed", "accounts")
        return _ok(f"Removed {name}.")
