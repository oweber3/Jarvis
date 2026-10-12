"""An Outlook.com or Microsoft 365 inbox through Microsoft Graph, read only, with a browser sign-in.

Outlook.com no longer accepts IMAP passwords, so this is the Microsoft online option. The sign-in
(``accounts/oauth.py``) grants ``Mail.Read`` at most, so Jarvis cannot send, move or delete mail with it.
Access tokens come from the sign-in's shared session (``accounts/logins.py``) and live in memory only.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

from ..accounts import http
from ..debug import debug_log
from . import messages as m

MAX_LIST = 25
_SELECT = "id,subject,from,receivedDateTime,isRead,bodyPreview"


class GraphAccount:
    kind = "graph"

    def __init__(self, name: str, *, session: Any, transport: Any = None, base: Optional[str] = None):
        self.name = name
        self._session = session
        self._transport = transport
        self._base = base

    def _get(self, path: str, params: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        return http.bearer_get(self._session, f"{self._base or http.GRAPH_BASE}{path}", params, extra,
                               transport=self._transport)

    def _summary(self, item: Dict[str, Any]) -> m.MailSummary:
        sender = (item.get("from") or {}).get("emailAddress") or {}
        name, address = m.one_line(sender.get("name") or ""), m.one_line(sender.get("address") or "")
        return m.MailSummary(self.name, str(item.get("id") or ""), name or address, address,
                             m.one_line(item.get("subject") or ""), _when(item.get("receivedDateTime")),
                             item.get("isRead") is False, m.snippet(item.get("bodyPreview") or ""))

    def list_messages(self, *, unread_only: bool = False, sender: str = "", limit: int = 10) -> m.AccountResult:
        limit = max(1, min(int(limit or 10), MAX_LIST))
        params: Dict[str, Any] = {"$select": _SELECT, "$orderby": "receivedDateTime desc"}
        # Graph wants the ordered property filtered first when both are given.
        params["$filter"] = ("receivedDateTime ge 1900-01-01T00:00:00Z" +
                             (" and isRead eq false" if unread_only else ""))
        # The sender is matched here, across a wider page, the same way for every account kind.
        params["$top"] = min(100, limit * 5) if sender else limit
        try:
            data = self._get("/me/mailFolders/inbox/messages", params)
        except http.ApiError as exc:
            return m.AccountResult(self.name, self.kind, False, error=exc.code)
        found = [self._summary(item) for item in data.get("value") or [] if isinstance(item, dict)]
        if sender:
            found = [s for s in found if m.matches_sender(sender, s.sender_name, s.sender_address)]
        if unread_only:
            found = [s for s in found if s.unread]
        debug_log(f"graph inbox: {len(found[:limit])} messages listed", "mail")
        return m.AccountResult(self.name, self.kind, True, found[:limit])

    def read_message(self, native_id: str) -> Optional[m.MailMessage]:
        if not native_id:
            return None
        try:
            item = self._get(f"/me/messages/{quote(native_id, safe='')}", {"$select": _SELECT + ",body"},
                             {"Prefer": 'outlook.body-content-type="text"'})
        except http.ApiError:
            return None
        body = (item.get("body") or {})
        content = str(body.get("content") or "")
        if str(body.get("contentType", "")).lower() == "html":
            content = m.html_to_text(content)
        text, truncated = m.cap_body(content)
        return m.MailMessage(**vars(self._summary(item)), body=text, truncated=truncated)

    def test(self) -> m.AccountResult:
        return self.list_messages(limit=1)


def microsoft_user(session: Any, transport: Any = None, base: Optional[str] = None) -> str:
    """The signed-in account's address (``User.Read``), or ``""``. Raises ``http.ApiError``."""
    data = http.bearer_get(session, f"{base or http.GRAPH_BASE}/me", {"$select": "mail,userPrincipalName"},
                           transport=transport)
    return m.one_line(data.get("mail") or data.get("userPrincipalName") or "")


def _when(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
