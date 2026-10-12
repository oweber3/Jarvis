"""An Outlook.com or Microsoft 365 inbox through Microsoft Graph, read only, signed in with the
OAuth device-code flow and the user's own Azure app (client) ID.

Outlook.com no longer accepts IMAP passwords, so this is the Microsoft online option. The only scopes
asked for are ``Mail.Read`` and ``offline_access``: Jarvis cannot send, move or delete mail with them.
The refresh token is kept in the credential store (``get_token`` / ``set_token``), replaced whenever
Microsoft rotates it, and never logged. Access tokens live in memory only.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import quote

from ..debug import debug_log
from . import messages as m

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access https://graph.microsoft.com/Mail.Read"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT_SEC = 15.0
MAX_LIST = 25
_SELECT = "id,subject,from,receivedDateTime,isRead,bodyPreview"


class RequestsTransport:
    """``post`` and ``get`` over HTTPS returning ``(status, json)``; network failures return status 0."""

    def post(self, url: str, data: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        import requests
        try:
            response = requests.post(url, data=data, timeout=TIMEOUT_SEC)
            return response.status_code, _json(response)
        except Exception as exc:  # noqa: BLE001 - every network failure means the same here
            debug_log(f"graph request failed ({type(exc).__name__})", "mail")
            return 0, {}

    def get(self, url: str, params: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        import requests
        try:
            response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT_SEC)
            return response.status_code, _json(response)
        except Exception as exc:  # noqa: BLE001
            debug_log(f"graph request failed ({type(exc).__name__})", "mail")
            return 0, {}


def _json(response) -> Dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _authority(tenant: str) -> str:
    return f"{AUTHORITY}/{tenant or 'common'}/oauth2/v2.0"


def device_sign_in(client_id: str, tenant: str, *, set_token: Callable[[str], bool], show: Callable[[str], None],
                   transport: Any = None, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> bool:
    """Sign in with a device code: ``show`` gets Microsoft's instruction (a web address and a code to
    enter there), then this waits for the sign-in and stores the refresh token. Returns success."""
    transport = transport or RequestsTransport()
    status, start = transport.post(f"{_authority(tenant)}/devicecode", {"client_id": client_id, "scope": SCOPES})
    if status != 200 or not start.get("device_code"):
        debug_log(f"graph device code refused (status {status}, {start.get('error', '')})", "mail")
        return False
    show(str(start.get("message") or f"Open {start.get('verification_uri')} and enter {start.get('user_code')}"))
    interval = max(0.0, float(start.get("interval", 5)))
    deadline = clock() + float(start.get("expires_in", 900))
    while clock() < deadline:
        sleep(interval)
        status, token = transport.post(f"{_authority(tenant)}/token", {
            "grant_type": DEVICE_GRANT, "client_id": client_id, "device_code": start["device_code"]})
        if status == 200 and token.get("refresh_token"):
            debug_log("graph sign-in completed", "mail")
            return bool(set_token(token["refresh_token"]))
        error = token.get("error", "")
        if error == "slow_down":
            interval += 5
        elif error != "authorization_pending":
            debug_log(f"graph sign-in failed ({error or status})", "mail")
            return False
    return False


class GraphAccount:
    kind = "graph"

    def __init__(self, name: str, *, client_id: str, tenant: str, get_token: Callable[[], str],
                 set_token: Callable[[str], bool], transport: Any = None):
        self.name = name
        self.client_id = client_id
        self.tenant = tenant or "common"
        self._get_token = get_token
        self._set_token = set_token
        self._transport = transport or RequestsTransport()
        self._access: Optional[Tuple[str, float]] = None

    def _access_token(self) -> str:
        if self._access and self._access[1] > time.time() + 60:
            return self._access[0]
        refresh = self._get_token()
        if not refresh:
            raise _Failure("not_signed_in")
        status, token = self._transport.post(f"{_authority(self.tenant)}/token", {
            "grant_type": "refresh_token", "client_id": self.client_id, "refresh_token": refresh, "scope": SCOPES})
        if status == 0:
            raise _Failure("unreachable")
        if status != 200 or not token.get("access_token"):
            debug_log(f"graph token refresh refused ({token.get('error', status)})", "mail")
            raise _Failure("not_signed_in")
        if token.get("refresh_token") and token["refresh_token"] != refresh:
            self._set_token(token["refresh_token"])
        self._access = (token["access_token"], time.time() + float(token.get("expires_in", 3600)))
        return self._access[0]

    def _get(self, path: str, params: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._access_token()}", **(extra or {})}
        status, data = self._transport.get(f"{GRAPH}{path}", params, headers)
        if status == 0:
            raise _Failure("unreachable")
        if status == 401:
            self._access = None
            raise _Failure("not_signed_in")
        if status != 200:
            raise _Failure("server_error")
        return data

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
        except _Failure as exc:
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
        except _Failure:
            return None
        body = (item.get("body") or {})
        content = str(body.get("content") or "")
        if str(body.get("contentType", "")).lower() == "html":
            content = m.html_to_text(content)
        text, truncated = m.cap_body(content)
        return m.MailMessage(**vars(self._summary(item)), body=text, truncated=truncated)

    def test(self) -> m.AccountResult:
        return self.list_messages(limit=1)


def _when(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


class _Failure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
