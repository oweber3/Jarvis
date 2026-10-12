"""Stored sign-ins and the access tokens made from them.

A sign-in (a "login") is one Microsoft or Google account the user signed in to. Its record (provider,
the OAuth client it was issued to, the scopes, the tenant and the refresh token) lives only in Windows
Credential Manager under ``oauth_login:<login id>``; config entries refer to it by its id. Keeping the
client in the record means a sign-in keeps working even if a later build ships a different client ID.

``session_for(login_id)`` is shared by every calendar and mailbox of that sign-in, so one access token
(memory only) serves them all. A rotated refresh token replaces the stored one. See ``accounts.spec.md``.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from .. import credentials
from ..debug import debug_log
from . import oauth

LOGIN_KIND = "oauth_login"
_REFRESH_MARGIN_SEC = 60.0


class LoginError(Exception):
    """No access token. ``code`` is ``not_signed_in`` (missing, revoked or expired: sign in again),
    ``unreachable`` or ``server_error``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class LoginRecord:
    provider: str
    client_id: str
    client_secret: str
    scopes: str
    refresh_token: str
    tenant: str = "common"

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Optional["LoginRecord"]:
        try:
            data = json.loads(text)
            record = cls(**{key: str(data.get(key) or "") for key in
                            ("provider", "client_id", "client_secret", "scopes", "refresh_token", "tenant")})
        except (ValueError, TypeError, AttributeError):
            return None
        record.tenant = record.tenant or "common"
        return record if record.provider and record.client_id and record.refresh_token else None

    @classmethod
    def from_tokens(cls, provider_key: str, client: oauth.Client, tokens: oauth.Tokens,
                    scopes: str = "", tenant: str = "common") -> "LoginRecord":
        return cls(provider_key, client.client_id, client.client_secret, scopes or tokens.scope,
                   tokens.refresh_token, tenant)


def new_login_id(provider_key: str) -> str:
    return f"{provider_key}-{secrets.token_hex(4)}"


def _key(login_id: str) -> str:
    return credentials.account_key(LOGIN_KIND, login_id)


def save(login_id: str, record: LoginRecord) -> bool:
    return credentials.set_secret(_key(login_id), record.to_json())


def load(login_id: str) -> Optional[LoginRecord]:
    if not login_id:
        return None
    text = credentials.get_secret(_key(login_id))
    return LoginRecord.from_json(text) if text else None


def delete(login_id: str) -> bool:
    with _lock:
        _sessions.pop(login_id, None)
    return credentials.delete_secret(_key(login_id))


class LoginSession:
    """Access tokens for one sign-in, refreshed when needed and kept in memory only."""

    def __init__(self, login_id: str, transport: Any = None, clock=time.time):
        self.login_id = login_id
        self._transport = transport
        self._clock = clock
        self._access: Optional[tuple] = None
        self._lock = threading.Lock()

    def prime(self, tokens: oauth.Tokens) -> None:
        """Use the access token a sign-in just returned, so the first read needs no refresh."""
        with self._lock:
            self._access = (tokens.access_token, tokens.expires_at)

    def invalidate(self) -> None:
        with self._lock:
            self._access = None

    def access_token(self) -> str:
        with self._lock:  # one refresh at a time: a rotated refresh token must not be used twice
            if self._access and self._access[1] > self._clock() + _REFRESH_MARGIN_SEC:
                return self._access[0]
            record = load(self.login_id)
            provider = oauth.PROVIDERS.get(record.provider) if record else None
            if record is None or provider is None:
                raise LoginError("not_signed_in")
            try:
                tokens = oauth.refresh(provider, oauth.Client(record.client_id, record.client_secret),
                                       record.refresh_token, record.scopes, tenant=record.tenant,
                                       transport=self._transport)
            except oauth.OAuthError as exc:
                raise LoginError(exc.code) from None
            if tokens.refresh_token and tokens.refresh_token != record.refresh_token:
                # Only replace the record this refresh started from: a sign-in removed or replaced
                # meanwhile (Settings runs in another process) must not come back.
                current = load(self.login_id)
                if current is None or current.refresh_token != record.refresh_token:
                    debug_log("sign-in changed during a refresh; rotated token not stored", "accounts")
                else:
                    record.refresh_token = tokens.refresh_token
                    if not save(self.login_id, record):
                        debug_log("rotated refresh token could not be stored", "accounts")
            self._access = (tokens.access_token, tokens.expires_at)
            return tokens.access_token


_lock = threading.Lock()
_sessions: Dict[str, LoginSession] = {}


def session_for(login_id: str) -> LoginSession:
    """The process-wide session of a sign-in."""
    with _lock:
        session = _sessions.get(login_id)
        if session is None:
            session = _sessions[login_id] = LoginSession(login_id)
        return session


def forget_sessions() -> None:
    with _lock:
        _sessions.clear()
