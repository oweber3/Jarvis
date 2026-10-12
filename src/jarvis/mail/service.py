"""Mail across every configured account: accounts from config, merged listings and short ids.

A listed message gets a short handle (8 hexadecimal characters derived from its account and the
account's own id) that the model can pass back to read it. Handles are kept in memory only, for the
most recent ``MAX_HANDLES`` messages. See ``mail.spec.md``.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

from ..debug import debug_log
from . import messages as m

ACCOUNT_TYPES = ("imap", "outlook", "graph")
MAX_HANDLES = 300
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_handles: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()
_lock = threading.Lock()


@dataclass
class Listing:
    messages: List[m.MailSummary] = field(default_factory=list)
    statuses: List[m.AccountResult] = field(default_factory=list)


def handle_for(account: str, native_id: str) -> str:
    handle = hashlib.sha256(f"{account}\x00{native_id}".encode("utf-8")).hexdigest()[:8]
    with _lock:
        _handles[handle] = (account, native_id)
        _handles.move_to_end(handle)
        while len(_handles) > MAX_HANDLES:
            _handles.popitem(last=False)
    return handle


def _lookup(handle: str) -> Optional[Tuple[str, str]]:
    with _lock:
        return _handles.get(str(handle or "").strip().lower())


def accounts_from_config(cfg: Any) -> list:
    """One account per entry of ``email_accounts``; passwords and tokens come from the credential store."""
    from .. import credentials
    from .graph import GraphAccount
    from .imap import ImapAccount
    from .outlook import OutlookMailAccount
    built = []
    for entry in getattr(cfg, "email_accounts", None) or []:
        name, kind = entry.get("name"), entry.get("type")
        if kind == "imap":
            key = credentials.account_key("email_password", name)
            built.append(ImapAccount(name, host=entry.get("host") or "imap.gmail.com", port=entry.get("port") or 993,
                                     username=entry.get("username") or "",
                                     password=lambda key=key: credentials.get_secret(key)))
        elif kind == "outlook":
            built.append(OutlookMailAccount(name))
        elif kind == "graph":
            key = credentials.account_key("email_graph_token", name)
            built.append(GraphAccount(name, client_id=entry.get("client_id") or "", tenant=entry.get("tenant") or "common",
                                      get_token=lambda key=key: credentials.get_secret(key),
                                      set_token=lambda value, key=key: credentials.set_secret(key, value)))
    return built


def _sort_key(summary: m.MailSummary):
    return summary.received or _EPOCH


def list_messages(accounts: Sequence[Any], *, unread_only: bool = False, sender: str = "",
                  limit: int = 10) -> Listing:
    """The newest messages across ``accounts``, newest first, each with a handle."""
    if not accounts:
        return Listing()

    def one(account) -> m.AccountResult:
        try:
            return account.list_messages(unread_only=unread_only, sender=sender, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one broken account must not hide the others
            debug_log(f"mail account failed ({type(exc).__name__})", "mail")
            return m.AccountResult(account.name, getattr(account, "kind", "?"), False, error="failed")

    with ThreadPoolExecutor(max_workers=min(4, len(accounts)), thread_name_prefix="mail") as pool:
        results = list(pool.map(one, accounts))
    merged = [msg for result in results for msg in result.messages]
    merged.sort(key=_sort_key, reverse=True)
    merged = merged[:max(1, int(limit or 10))]
    for msg in merged:
        msg.handle = handle_for(msg.account, msg.native_id)
    debug_log(f"mail: {len(merged)} messages from {sum(r.ok for r in results)}/{len(results)} accounts", "mail")
    return Listing(merged, results)


def read_message(accounts: Sequence[Any], handle: str) -> Optional[m.MailMessage]:
    """The message a handle from an earlier listing names, or None."""
    found = _lookup(handle)
    if found is None:
        return None
    account_name, native_id = found
    for account in accounts:
        if account.name == account_name:
            message = account.read_message(native_id)
            if message is not None:
                message.handle = str(handle).strip().lower()
            return message
    return None


def read_latest(accounts: Sequence[Any], *, sender: str = "", unread_only: bool = False
                ) -> Optional[m.MailMessage]:
    """The newest message (from ``sender`` when given) across the accounts, read in full."""
    listing = list_messages(accounts, unread_only=unread_only, sender=sender, limit=1)
    if not listing.messages:
        return None
    return read_message(accounts, listing.messages[0].handle)
