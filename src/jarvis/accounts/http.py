"""HTTPS calls for the online account kinds, returning ``(status, json)``; a network failure is status 0.

Tokens travel only in request bodies and the ``Authorization`` header and are never logged.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from ..debug import debug_log

TIMEOUT_SEC = 15.0


class Transport:
    """``post`` (form encoded) and ``get`` over HTTPS with a time limit."""

    def post(self, url: str, data: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        import requests
        try:
            response = requests.post(url, data=data, timeout=TIMEOUT_SEC, allow_redirects=False)
            return response.status_code, _json(response)
        except Exception as exc:  # noqa: BLE001 - every network failure means the same here
            debug_log(f"account request failed ({type(exc).__name__})", "accounts")
            return 0, {}

    def get(self, url: str, params: Dict[str, Any], headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        import requests
        try:
            response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT_SEC)
            return response.status_code, _json(response)
        except Exception as exc:  # noqa: BLE001
            debug_log(f"account request failed ({type(exc).__name__})", "accounts")
            return 0, {}


def _json(response) -> Dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


class ApiError(Exception):
    """A read that failed. ``code`` is ``not_signed_in`` (sign in again), ``unreachable`` or ``server_error``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def bearer_get(session: Any, url: str, params: Dict[str, Any] = None, headers: Dict[str, str] = None,
               transport: Any = None) -> Dict[str, Any]:
    """GET ``url`` with the sign-in's access token. A refused token (401) is refreshed once before the read
    counts as signed out (an access token can be revoked before it expires). A 403 or 429 (rate limits, no
    mailbox licence) is a ``server_error`` and keeps the token."""
    from .logins import LoginError
    transport = transport or Transport()
    for attempt in (1, 2):
        try:
            token = session.access_token()
        except LoginError as exc:
            raise ApiError(exc.code) from None
        status, data = transport.get(url, dict(params or {}), {"Authorization": f"Bearer {token}", **(headers or {})})
        if status == 0:
            raise ApiError("unreachable")
        if status == 401:
            session.invalidate()
            if attempt == 1:
                continue
            debug_log("account read refused (status 401)", "accounts")
            raise ApiError("not_signed_in")
        if status != 200:
            debug_log(f"account read failed (status {status})", "accounts")
            raise ApiError("server_error")
        return data
    raise ApiError("not_signed_in")


def same_origin(url: str, base: str) -> bool:
    """Whether a paging link points at the same scheme, host and port as ``base`` (so a token is never sent
    elsewhere)."""
    from urllib.parse import urlsplit
    a, b = urlsplit(url), urlsplit(base)
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())
