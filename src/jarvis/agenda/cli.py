"""Add, list, test and remove calendars from a terminal: ``python -m jarvis.agenda setup | list | test [name] |
remove <name>``. Settings > Calendar & Email does the same without a terminal.

Microsoft and Google accounts sign in in the browser. Secret addresses are typed hidden. Both go straight to
the credential store and are never printed or written to ``config.json``. Adding a calendar turns the
calendar on. See ``agenda.spec.md``.
"""
from __future__ import annotations

import getpass
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from .. import credentials
from ..accounts.manage import CALENDAR_ERRORS as ERRORS
from ..accounts.manage import AccountStore
from . import service
from .sources import IcsSource, OutlookCalendarSource, normalise_address

USAGE = "ℹ️ Usage: python -m jarvis.agenda setup | list | test [name] | remove <name>"
KINDS = [
    ("microsoft_signin", "Microsoft account: Outlook.com or Microsoft 365 (sign in in your browser)"),
    ("google_signin", "Google account (sign in in your browser)"),
    ("google", "Google Calendar by its secret address in iCal format"),
    ("outlookcom", "Outlook.com or Microsoft 365 by a published calendar ICS link"),
    ("ics", "Any other calendar address (.ics or webcal://)"),
    ("outlook", "Classic Outlook on this PC"),
]
HOW_TO = {
    "google": ["On a computer, open calendar.google.com > Settings (⚙️) > the calendar under",
               "\"Settings for my calendars\" > Integrate calendar > Secret address in iCal format > copy."],
    "outlookcom": ["Open outlook.live.com (or Outlook on the web) > Settings > Calendar > Shared calendars >",
                   "Publish a calendar > choose the calendar and \"Can view all details\" > Publish > copy the ICS link."],
    "ics": ["Copy the calendar's subscription address (it usually ends in .ics or starts with webcal://)."],
}


class Io:
    def __init__(self, ask: Callable[[str], str] = input, ask_secret: Callable[[str], str] = getpass.getpass,
                 out: Callable[[str], None] = print, store: Optional[AccountStore] = None):
        self.ask, self.ask_secret, self.out = ask, ask_secret, out
        self.store = store  # test seam: the account store (browser sign-in and its endpoints)

    def accounts(self) -> AccountStore:
        return self.store or AccountStore()


def _entries(cfg: Any) -> List[Dict[str, Any]]:
    return [dict(e) for e in getattr(cfg, "calendar_sources", None) or []]


def _save(entries: List[Dict[str, Any]], *, enable: Optional[bool] = None) -> bool:
    from ..config import update_config_values
    updates: Dict[str, Any] = {"calendar_sources": entries}
    if enable is not None:
        updates["calendar_enabled"] = enable
    return update_config_values(updates)


def _check(source, io: Io) -> bool:
    now = datetime.now().astimezone()
    result = source.read(now, now + timedelta(days=7))
    if result.ok and not result.offline_copy:
        io.out(f"   ✅ Connected: {len(result.occurrences)} events in the next 7 days")
        return True
    io.out(f"   ❌ Could not read it: {ERRORS.get(result.error, 'unknown error')}")
    return False


def _choose(io: Io) -> Optional[str]:
    io.out("📅 Add a calendar")
    for number, (_key, label) in enumerate(KINDS, start=1):
        io.out(f"   {number}. {label}")
    answer = io.ask(f"   👉 Choose 1-{len(KINDS)}: ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(KINDS):
        return KINDS[int(answer) - 1][0]
    io.out("   ❌ Not a choice.")
    return None


def setup(cfg: Any, io: Io) -> int:
    kind = _choose(io)
    if kind is None:
        return 1
    if kind.endswith("_signin"):
        provider = kind[:-len("_signin")]
        io.out(f"   🌐 Your browser opens {provider.capitalize()}'s sign-in page. Sign in there and allow "
               "reading your calendar.")
        io.out("   ⏳ Waiting up to 3 minutes...")
        outcome = io.accounts().connect_oauth(provider, calendar=True, mail=False)
        io.out(f"   {'✅' if outcome.ok else '❌'} {outcome.message}")
        if outcome.ok:
            io.out("   🔄 Restart Jarvis to use it.")
        return 0 if outcome.ok else 1
    entries = _entries(cfg)
    taken = {e["name"].casefold() for e in entries}
    default = {"google": "Google", "outlookcom": "Outlook.com", "ics": "Calendar", "outlook": "Outlook"}[kind]
    name = io.ask(f"   🏷️ Name for this calendar [{default}]: ").strip() or default
    if "#" in name:
        io.out("   ❌ A name cannot contain #.")
        return 1
    if name.casefold() in taken:
        io.out(f"   ❌ A calendar called {name} already exists. Remove it first or pick another name.")
        return 1
    if kind == "outlook":
        source = OutlookCalendarSource(name)
        io.out("   🔎 Reading classic Outlook...")
        if not _check(source, io):
            return 1
        entries.append({"name": name, "type": "outlook"})
    else:
        io.out("   📋 How to find the address:")
        for line in HOW_TO[kind]:
            io.out(f"      {line}")
        io.out("   🔒 The address works like a password: anyone with it can read the calendar.")
        url = io.ask_secret("   🔑 Paste the address (hidden): ").strip()
        try:
            url = normalise_address(url)
        except ValueError as exc:
            io.out(f"   ❌ {exc}")
            return 1
        key = credentials.account_key("calendar_url", name)
        if not credentials.set_secret(key, url):
            io.out("   ❌ Windows Credential Manager could not store the address. Nothing was saved.")
            return 1
        io.out("   🔎 Testing the address...")
        source = IcsSource(name, cache_dir=service.cache_dir(cfg), ttl_sec=0,
                           address=lambda: credentials.get_secret(key))
        if not _check(source, io):
            credentials.delete_secret(key)
            source.forget_copy()
            return 1
        entries.append({"name": name, "type": "ics"})
    if not _save(entries, enable=True):
        io.out("   ❌ The settings file could not be written.")
        return 1
    io.out(f"   💾 Saved {name}. The calendar is on.")
    io.out("   🔄 Restart Jarvis to use it.")
    return 0


def list_sources(cfg: Any, io: Io) -> int:
    entries = _entries(cfg)
    state = "on" if getattr(cfg, "calendar_enabled", False) else "off"
    io.out(f"📅 Calendars: {len(entries)} (calendar reading is {state})")
    for entry in entries:
        if entry["type"] == "ics":
            stored = credentials.has_secret(credentials.account_key("calendar_url", entry["name"]))
            io.out(f"   🗓️ {entry['name']}  (ICS address, {'stored' if stored else '⚠️ missing'})")
        elif entry["type"] in ("microsoft", "google"):
            from ..accounts import logins
            signed_in = logins.load(entry.get("login", "")) is not None
            io.out(f"   🗓️ {entry['name']}  ({entry['type'].capitalize()} account, "
                   f"{'signed in' if signed_in else '⚠️ not signed in'})")
        else:
            io.out(f"   🗓️ {entry['name']}  (classic Outlook)")
    return 0


def test(cfg: Any, io: Io, name: Optional[str]) -> int:
    sources = [s for s in service.sources_from_config(cfg) if name is None or s.name.casefold() == name.casefold()]
    if not sources:
        io.out("❌ No calendar by that name." if name else "❌ No calendars are set up.")
        return 1
    worked = True
    for source in sources:
        io.out(f"📅 {source.name}")
        if isinstance(source, IcsSource):
            source = IcsSource(source.name, cache_dir=service.cache_dir(cfg), ttl_sec=0, address=source.address)
        worked = _check(source, io) and worked
    return 0 if worked else 1


def remove(cfg: Any, io: Io, name: str) -> int:
    entries = _entries(cfg)
    kept = [e for e in entries if e["name"].casefold() != name.casefold()]
    if len(kept) == len(entries):
        io.out(f"❌ No calendar called {name}.")
        return 1
    removed = next(e for e in entries if e["name"].casefold() == name.casefold())
    outcome = io.accounts().remove("calendar", removed["name"])
    if not outcome.ok:
        io.out(f"❌ {outcome.message}")
        return 1
    io.out(f"🗑️ Removed {removed['name']} with its stored address, sign-in or saved copy.")
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
    if command == "setup":
        return setup(cfg, io)
    if command == "list":
        return list_sources(cfg, io)
    if command == "test":
        return test(cfg, io, " ".join(args[1:]) or None)
    if command == "remove" and len(args) >= 2:
        return remove(cfg, io, " ".join(args[1:]))
    io.out(USAGE)
    return 2
