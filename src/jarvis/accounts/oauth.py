"""Browser sign-in: OAuth 2.0 authorisation code with PKCE (RFC 7636) and a loopback redirect (RFC 8252).

``authorize`` opens the provider's sign-in page in the system browser and waits on a one-shot HTTP
receiver bound to 127.0.0.1 on a random port. The receiver answers exactly one request on its callback
path, checks ``state`` in constant time, never echoes anything from the request into its page, and is
closed when the code arrives, on cancel or after the time limit. The code is then exchanged, with the
PKCE verifier, for a refresh token. Only read scopes are ever asked for. Codes, tokens and addresses are
never logged. See ``accounts.spec.md``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

from ..debug import debug_log
from .clients import BUILT_IN_OAUTH_CLIENTS

SIGN_IN_TIMEOUT_SEC = 180.0
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
MICROSOFT_BASE_SCOPES = ("offline_access", "User.Read")
MICROSOFT_CALENDAR_SCOPE = "Calendars.Read"
MICROSOFT_MAIL_SCOPE = "Mail.Read"
# Scopes a token response may leave out without anything missing (they add no data access).
_UNLISTED_SCOPES = {"offline_access", "openid", "profile", "email"}


class OAuthError(Exception):
    """A sign-in that did not finish. ``code`` is one of: not_configured, browser, cancelled, timeout,
    state_mismatch, denied, provider_error, no_code, unreachable, token_refused, scope_missing,
    not_signed_in, server_error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    authorize_url: str
    # ``{tenant}`` is filled in for Microsoft; Google's address has no placeholder.
    token_url: str
    # The loopback name in the redirect address: Microsoft registers ``http://localhost``, Google's
    # desktop clients use ``127.0.0.1``. The receiver itself always listens on 127.0.0.1 only.
    redirect_host: str
    auth_params: Tuple[Tuple[str, str], ...] = ()
    # Microsoft wants the scopes again on a refresh; Google does not.
    refresh_scope: bool = False


MICROSOFT = Provider(
    "microsoft", "Microsoft",
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
    "localhost", (("prompt", "select_account"),), refresh_scope=True)
GOOGLE = Provider(
    "google", "Google",
    "https://accounts.google.com/o/oauth2/v2/auth",
    "https://oauth2.googleapis.com/token",
    "127.0.0.1", (("access_type", "offline"), ("prompt", "consent")))
PROVIDERS: Dict[str, Provider] = {"microsoft": MICROSOFT, "google": GOOGLE}
_CLIENT_KEYS = {"microsoft": ("microsoft_oauth_client_id", ""),
                "google": ("google_oauth_client_id", "google_oauth_client_secret")}


@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id.strip())


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str


def client_for(provider_key: str, cfg: Any = None) -> Client:
    """The OAuth client for ``provider_key``: the config value when set, otherwise the built-in one."""
    id_key, secret_key = _CLIENT_KEYS[provider_key]

    def value(key: str) -> str:
        if not key:
            return ""
        found = cfg.get(key) if isinstance(cfg, dict) else getattr(cfg, key, None)
        text = found.strip() if isinstance(found, str) else ""
        return text or BUILT_IN_OAUTH_CLIENTS.get(key, "")

    return Client(value(id_key), value(secret_key))


def scopes_for(provider: Provider, calendar: bool, mail: bool) -> str:
    """The read-only scopes for what the user ticked. Google is calendar only (Gmail uses an app password)."""
    if provider.key == "google":
        return GOOGLE_CALENDAR_SCOPE
    wanted = list(MICROSOFT_BASE_SCOPES)
    if calendar:
        wanted.append(MICROSOFT_CALENDAR_SCOPE)
    if mail:
        wanted.append(MICROSOFT_MAIL_SCOPE)
    return " ".join(wanted)


def pkce_pair() -> Tuple[str, str]:
    """A fresh code verifier (64 URL-safe characters) and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── Loopback receiver ──────────────────────────────────────────────────────

_PAGE = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Jarvis</title></head>"
         "<body style=\"font-family:Segoe UI,sans-serif;margin:3em;max-width:36em\"><h2>{title}</h2><p>{text}</p>"
         "</body></html>")
_SIGNED_IN = ("Signed in", "Jarvis has your permission now. You can close this tab and go back to Jarvis.")
_NOT_SIGNED_IN = ("Not signed in", "The sign-in did not finish. Close this tab and try again from Jarvis.")
_NOT_FOUND = ("Not found", "This address is only used to finish signing in to Jarvis.")


class _Server(HTTPServer):
    allow_reuse_address = False  # SO_REUSEADDR on Windows would let another program take over the port

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind looks the host name up in DNS, which can stall; it is not needed here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Handler(BaseHTTPRequestHandler):
    server_version = "Jarvis"
    sys_version = ""
    timeout = 5  # a connection that never sends a request cannot hold the sign-in

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        receiver: "LoopbackReceiver" = self.server.receiver  # type: ignore[attr-defined]
        parts = urlsplit(self.path)
        if parts.path != "/" or receiver.result is not None:
            self._page(404, _NOT_FOUND)
            return
        query = parse_qs(parts.query)
        state = (query.get("state") or [""])[0]
        if not hmac.compare_digest(state.encode("utf-8"), receiver.state.encode("utf-8")):
            receiver.result = ("error", "state_mismatch")
        elif query.get("error"):
            receiver.result = ("error", "denied" if query["error"][0] == "access_denied" else "provider_error")
        elif (query.get("code") or [""])[0]:
            receiver.result = ("code", query["code"][0])
        else:
            receiver.result = ("error", "no_code")
        self._page(200 if receiver.result[0] == "code" else 400,
                   _SIGNED_IN if receiver.result[0] == "code" else _NOT_SIGNED_IN)

    def _page(self, status: int, words: Tuple[str, str]) -> None:
        data = _PAGE.format(title=words[0], text=words[1]).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass  # the request line holds the authorisation code


class LoopbackReceiver:
    """A one-shot HTTP receiver on 127.0.0.1 and a random free port, for the sign-in redirect."""

    def __init__(self, redirect_host: str = "127.0.0.1"):
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.receiver = self  # type: ignore[attr-defined]
        self.address: Tuple[str, int] = self._server.server_address[:2]
        self.redirect_uri = f"http://{redirect_host}:{self.address[1]}"
        self.state = ""
        self.result: Optional[Tuple[str, str]] = None

    def wait(self, state: str, *, timeout: float, cancel: threading.Event) -> str:
        """The authorisation code from the first redirect, or ``OAuthError``."""
        self.state = state
        deadline = time.monotonic() + timeout
        while self.result is None:
            remaining = deadline - time.monotonic()
            if cancel.is_set():
                raise OAuthError("cancelled")
            if remaining <= 0:
                raise OAuthError("timeout")
            self._server.timeout = min(0.25, remaining)
            self._server.handle_request()
        kind, value = self.result
        if kind != "code":
            raise OAuthError(value)
        return value

    def close(self) -> None:
        self._server.server_close()


# ── The flow ───────────────────────────────────────────────────────────────

def _open_browser(url: str) -> bool:
    import webbrowser
    return bool(webbrowser.open(url))


def _tokens(body: Dict[str, Any], refresh_token: str = "") -> Tokens:
    return Tokens(str(body["access_token"]), str(body.get("refresh_token") or refresh_token),
                  time.time() + float(body.get("expires_in") or 3600), str(body.get("scope") or ""))


def _normalised_scopes(text: str) -> set:
    return {s.rsplit("/", 1)[-1].casefold() if s.startswith("https://graph.microsoft.com/") else s.casefold()
            for s in text.split()}


def _check_scopes(asked: str, granted: str) -> None:
    """A provider that lists the granted scopes must list every data scope asked for (Google lets the user
    untick one on its consent page)."""
    if not granted:
        return
    missing = _normalised_scopes(asked) - _normalised_scopes(granted) - _UNLISTED_SCOPES
    if missing:
        debug_log(f"sign-in granted {len(missing)} fewer scopes than asked", "accounts")
        raise OAuthError("scope_missing")


def authorize(provider: Provider, client: Client, scopes: str, *,
              open_browser: Optional[Callable[[str], bool]] = None, cancel: Optional[threading.Event] = None,
              timeout: float = SIGN_IN_TIMEOUT_SEC, transport: Any = None, tenant: str = "common") -> Tokens:
    """Sign in through the system browser and return the tokens. Raises ``OAuthError``."""
    if not client.configured:
        raise OAuthError("not_configured")
    from .http import Transport
    transport = transport or Transport()
    cancel = cancel or threading.Event()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    receiver = LoopbackReceiver(provider.redirect_host)
    try:
        params = {"client_id": client.client_id, "response_type": "code", "redirect_uri": receiver.redirect_uri,
                  "scope": scopes, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                  **dict(provider.auth_params)}
        debug_log(f"{provider.key} sign-in started", "accounts")
        if not (open_browser or _open_browser)(f"{provider.authorize_url}?{urlencode(params)}"):
            raise OAuthError("browser")
        code = receiver.wait(state, timeout=timeout, cancel=cancel)
    except OAuthError as exc:
        debug_log(f"{provider.key} sign-in stopped ({exc.code})", "accounts")
        raise
    finally:
        receiver.close()
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": receiver.redirect_uri,
            "client_id": client.client_id, "code_verifier": verifier}
    if client.client_secret:
        data["client_secret"] = client.client_secret
    if provider.refresh_scope:
        data["scope"] = scopes
    status, body = transport.post(provider.token_url.format(tenant=tenant), data)
    if status == 0:
        raise OAuthError("unreachable")
    if status != 200 or not body.get("access_token") or not body.get("refresh_token"):
        debug_log(f"{provider.key} code exchange refused (status {status}, {body.get('error', '')})", "accounts")
        raise OAuthError("token_refused")
    tokens = _tokens(body)
    _check_scopes(scopes, tokens.scope)
    debug_log(f"{provider.key} sign-in completed", "accounts")
    return tokens


def refresh(provider: Provider, client: Client, refresh_token: str, scopes: str, *, tenant: str = "common",
            transport: Any = None) -> Tokens:
    """A new access token (and possibly a rotated refresh token). Raises ``OAuthError`` with
    ``not_signed_in`` (refused: revoked, expired or a changed password), ``unreachable`` or ``server_error``."""
    from .http import Transport
    transport = transport or Transport()
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client.client_id}
    if client.client_secret:
        data["client_secret"] = client.client_secret
    if provider.refresh_scope and scopes:
        data["scope"] = scopes
    status, body = transport.post(provider.token_url.format(tenant=tenant or "common"), data)
    if status == 0:
        raise OAuthError("unreachable")
    if status in (400, 401) or (status == 200 and not body.get("access_token")):
        debug_log(f"{provider.key} token refresh refused ({body.get('error', status)})", "accounts")
        raise OAuthError("not_signed_in")
    if status != 200:
        debug_log(f"{provider.key} token refresh failed (status {status})", "accounts")
        raise OAuthError("server_error")
    return _tokens(body, refresh_token)
