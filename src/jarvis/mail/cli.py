"""Add, list, test and remove email accounts from a terminal: ``python -m jarvis.mail setup | list |
test [name] | remove <name>``. Settings > Calendar & Email does the same without a terminal.

Passwords are typed hidden and Microsoft accounts sign in in the browser; secrets go straight to the
credential store and are never printed or written to ``config.json``. Adding an account turns email
on. See ``mail.spec.md``.
"""
from __future__ import annotations

import getpass
import sys
from typing import Any, Callable, Dict, List, Optional

from .. import credentials
from ..accounts.manage import MAIL_ERRORS as ERRORS
from ..accounts.manage import AccountStore
from .imap import ImapAccount
from .outlook import OutlookMailAccount

USAGE = "ℹ️ Usage: python -m jarvis.mail setup | list | test [name] | remove <name>"
KINDS = [
    ("gmail", "Gmail (with an app password)"),
    ("imap", "Another IMAP mailbox"),
    ("outlook", "Classic Outlook on this PC"),
    ("microsoft", "Outlook.com or Microsoft 365 (sign in in your browser)"),
]
GMAIL_HOW_TO = [
    "Gmail needs an app password: a separate 16-letter password just for Jarvis.",
    "1. Turn on 2-Step Verification in your Google Account (myaccount.google.com > Security).",
    "2. Open myaccount.google.com/apppasswords, name it Jarvis, and copy the password it shows.",
    "You can revoke it there at any time without changing your Google password.",
]


class Io:
    def __init__(self, ask: Callable[[str], str] = input, ask_secret: Callable[[str], str] = getpass.getpass,
                 out: Callable[[str], None] = print, store: Optional[AccountStore] = None,
                 connect: Optional[Callable] = None):
        self.ask, self.ask_secret, self.out = ask, ask_secret, out
        # Test seams: the account store (browser sign-in and its endpoints) and the IMAP connection.
        self.store, self.connect = store, connect

    def accounts(self) -> AccountStore:
        return self.store or AccountStore()


def _entries(cfg: Any) -> List[Dict[str, Any]]:
    return [dict(e) for e in getattr(cfg, "email_accounts", None) or []]


def _save(entries: List[Dict[str, Any]], *, enable: Optional[bool] = None) -> bool:
    from ..config import update_config_values
    updates: Dict[str, Any] = {"email_accounts": entries}
    if enable is not None:
        updates["email_enabled"] = enable
    return update_config_values(updates)


def _check(account, io: Io) -> bool:
    result = account.test()
    if result.ok:
        unread = sum(1 for m in result.messages if m.unread)
        io.out(f"   ✅ Connected: the inbox can be read{' (newest message unread)' if unread else ''}")
        return True
    io.out(f"   ❌ Could not read it: {ERRORS.get(result.error, 'unknown error')}")
    return False


def _account(entry: Dict[str, Any], io: Io):
    """The account object for a config entry, with the CLI's test seams applied."""
    name = entry["name"]
    if entry["type"] == "imap":
        key = credentials.account_key("email_password", name)
        return ImapAccount(name, host=entry.get("host", "imap.gmail.com"), port=entry.get("port", 993),
                           username=entry.get("username", ""), password=lambda: credentials.get_secret(key),
                           connect=io.connect)
    if entry["type"] == "graph":
        return io.accounts()._account(entry)
    return OutlookMailAccount(name)


def _sign_in(io: Io) -> int:
    """Microsoft: sign in in the browser; the account store tests and saves it."""
    io.out("   🌐 Your browser opens Microsoft's sign-in page. Sign in there and allow reading your mail.")
    io.out("   ⏳ Waiting up to 3 minutes...")
    outcome = io.accounts().connect_oauth("microsoft", calendar=False, mail=True)
    if not outcome.ok:
        io.out(f"   ❌ {outcome.message}")
        return 1
    io.out(f"   ✅ {outcome.message}")
    io.out("   🔄 Restart Jarvis to use it.")
    return 0


def _choose(io: Io) -> Optional[str]:
    io.out("📧 Add an email account (read only: Jarvis never sends, deletes or marks mail)")
    for number, (_key, label) in enumerate(KINDS, start=1):
        io.out(f"   {number}. {label}")
    answer = io.ask("   👉 Choose 1-4: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(KINDS):
        return KINDS[int(answer) - 1][0]
    io.out("   ❌ Not a choice.")
    return None


def setup(cfg: Any, io: Io) -> int:
    kind = _choose(io)
    if kind is None:
        return 1
    if kind == "microsoft":
        return _sign_in(io)
    entries = _entries(cfg)
    taken = {e["name"].casefold() for e in entries}
    default = {"gmail": "Gmail", "imap": "Mail", "outlook": "Outlook"}[kind]
    name = io.ask(f"   🏷️ Name for this account [{default}]: ").strip() or default
    if "#" in name:
        io.out("   ❌ A name cannot contain #.")
        return 1
    if name.casefold() in taken:
        io.out(f"   ❌ An account called {name} already exists. Remove it first or pick another name.")
        return 1

    secret_key = None
    if kind in ("gmail", "imap"):
        if kind == "gmail":
            for line in GMAIL_HOW_TO:
                io.out(f"   📋 {line}")
            host, port = "imap.gmail.com", 993
        else:
            host = io.ask("   🖥️ IMAP server (for example imap.example.com): ").strip()
            port_text = io.ask("   🔢 Port [993]: ").strip() or "993"
            if not host or not port_text.isdigit():
                io.out("   ❌ A server name and a port number are needed.")
                return 1
            host, port = host, int(port_text)
        username = io.ask("   👤 Email address (sign-in name): ").strip()
        password = io.ask_secret("   🔑 App password (hidden): ").replace(" ", "").strip() if kind == "gmail" \
            else io.ask_secret("   🔑 Password (hidden): ").strip()
        if not username or not password:
            io.out("   ❌ An address and a password are needed.")
            return 1
        secret_key = credentials.account_key("email_password", name)
        if not credentials.set_secret(secret_key, password):
            io.out("   ❌ Windows Credential Manager could not store the password. Nothing was saved.")
            return 1
        entry: Dict[str, Any] = {"name": name, "type": "imap", "host": host, "port": port, "username": username}
    else:
        entry = {"name": name, "type": "outlook"}

    io.out("   🔎 Testing the account...")
    if not _check(_account(entry, io), io):
        if secret_key:
            credentials.delete_secret(secret_key)
        return 1
    entries.append(entry)
    if not _save(entries, enable=True):
        io.out("   ❌ The settings file could not be written.")
        return 1
    io.out(f"   💾 Saved {name}. Email reading is on.")
    io.out("   🔄 Restart Jarvis to use it.")
    return 0


def list_accounts(cfg: Any, io: Io) -> int:
    entries = _entries(cfg)
    state = "on" if getattr(cfg, "email_enabled", False) else "off"
    io.out(f"📧 Email accounts: {len(entries)} (email reading is {state})")
    for entry in entries:
        if entry["type"] == "imap":
            stored = credentials.has_secret(credentials.account_key("email_password", entry["name"]))
            io.out(f"   📮 {entry['name']}  (IMAP {entry.get('host', '')}, password "
                   f"{'stored' if stored else '⚠️ missing'})")
        elif entry["type"] == "graph":
            from ..accounts import logins
            stored = logins.load(entry.get("login", "")) is not None
            io.out(f"   📮 {entry['name']}  (Microsoft account, {'signed in' if stored else '⚠️ not signed in'})")
        else:
            io.out(f"   📮 {entry['name']}  (classic Outlook)")
    return 0


def _find(cfg: Any, name: str) -> Optional[Dict[str, Any]]:
    return next((e for e in _entries(cfg) if e["name"].casefold() == name.casefold()), None)


def test(cfg: Any, io: Io, name: Optional[str]) -> int:
    entries = [e for e in _entries(cfg) if name is None or e["name"].casefold() == name.casefold()]
    if not entries:
        io.out("❌ No account by that name." if name else "❌ No email accounts are set up.")
        return 1
    worked = True
    for entry in entries:
        io.out(f"📧 {entry['name']}")
        worked = _check(_account(entry, io), io) and worked
    return 0 if worked else 1


def remove(cfg: Any, io: Io, name: str) -> int:
    entry = _find(cfg, name)
    if entry is None:
        io.out(f"❌ No account called {name}.")
        return 1
    outcome = io.accounts().remove("email", entry["name"])
    if not outcome.ok:
        io.out(f"❌ {outcome.message}")
        return 1
    io.out(f"🗑️ Removed {entry['name']} and its stored password or sign-in.")
    return 0


def main(argv=None, io: Optional[Io] = None) -> int:
    from ..config import load_settings
    for stream in (sys.stdout, sys.stderr):  # a Windows console may not default to UTF-8, and lines start with emoji
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    io = io or Io()
    cfg = load_settings()
    command = args[0] if args else "list"
    rest = " ".join(args[1:])
    if command == "setup":
        return setup(cfg, io)
    if command == "list":
        return list_accounts(cfg, io)
    if command == "test":
        return test(cfg, io, rest or None)
    if command == "remove" and rest:
        return remove(cfg, io, rest)
    io.out(USAGE)
    return 2
