"""The Inbox of classic Outlook on this PC, read through COM. Read only: properties are read, nothing
is set, sent, moved or deleted. The new Outlook has no COM interface and is reported as such.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, List, Optional

from ..debug import debug_log
from ..platform.windows.outlook import OutlookUnavailable
from . import messages as m

MAX_LIST = 25
MAX_SCANNED = 300
_OL_FOLDER_INBOX = 6
_OL_MAIL = 43


def _local(moment: Any) -> Optional[datetime]:
    """An Outlook COM time (a local wall time, which pywin32 may label UTC) as an aware local time."""
    if moment is None:
        return None
    try:
        return datetime(moment.year, moment.month, moment.day, moment.hour, moment.minute,
                        moment.second).astimezone()
    except (AttributeError, ValueError, OverflowError):
        return None


class OutlookMailAccount:
    kind = "outlook"

    def __init__(self, name: str, *, runner: Optional[Callable] = None,
                 install_state: Optional[Callable[[], str]] = None):
        from ..platform.windows import outlook
        self.name = name
        self._runner = runner or (lambda fn, timeout: outlook.run_with_namespace(fn, timeout))
        self._install_state = install_state or outlook.install_state

    def _unavailable(self) -> str:
        state = self._install_state()
        if state == "classic":
            return ""
        return "outlook_new_only" if state == "new_only" else "outlook_not_installed"

    def _summary(self, item: Any, body: str) -> m.MailSummary:
        name = m.one_line(getattr(item, "SenderName", "") or "")
        address = m.one_line(getattr(item, "SenderEmailAddress", "") or "")
        return m.MailSummary(self.name, str(item.EntryID), name or address, address,
                             m.one_line(getattr(item, "Subject", "") or ""), _local(getattr(item, "ReceivedTime", None)),
                             bool(getattr(item, "UnRead", False)), m.snippet(body))

    def list_messages(self, *, unread_only: bool = False, sender: str = "", limit: int = 10) -> m.AccountResult:
        limit = max(1, min(int(limit or 10), MAX_LIST))
        problem = self._unavailable()
        if problem:
            return m.AccountResult(self.name, self.kind, False, error=problem)

        def collect(namespace) -> List[m.MailSummary]:
            items = namespace.GetDefaultFolder(_OL_FOLDER_INBOX).Items
            items.Sort("[ReceivedTime]", True)
            if unread_only:
                items = items.Restrict("[UnRead] = True")
            found = []
            for index, item in enumerate(items):
                if index >= MAX_SCANNED or len(found) >= limit:
                    break
                if getattr(item, "Class", _OL_MAIL) != _OL_MAIL:
                    continue
                if sender and not m.matches_sender(sender, str(getattr(item, "SenderName", "") or ""),
                                                   str(getattr(item, "SenderEmailAddress", "") or "")):
                    continue
                found.append(self._summary(item, str(getattr(item, "Body", "") or "")))
            return found

        try:
            found = self._runner(collect, 20.0)
        except OutlookUnavailable as exc:
            return m.AccountResult(self.name, self.kind, False, error=exc.reason)
        debug_log(f"outlook inbox: {len(found)} messages listed", "mail")
        return m.AccountResult(self.name, self.kind, True, found)

    def read_message(self, native_id: str) -> Optional[m.MailMessage]:
        if self._unavailable() or not native_id:
            return None

        def collect(namespace):
            item = namespace.GetItemFromID(native_id)
            body = str(getattr(item, "Body", "") or "")
            return self._summary(item, body), body

        try:
            summary, body = self._runner(collect, 20.0)
        except OutlookUnavailable:
            return None
        except Exception:  # noqa: BLE001 - an id Outlook no longer knows
            return None
        text, truncated = m.cap_body(body)
        return m.MailMessage(**vars(summary), body=text, truncated=truncated)

    def test(self) -> m.AccountResult:
        return self.list_messages(limit=1)
