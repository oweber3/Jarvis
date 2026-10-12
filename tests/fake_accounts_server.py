"""A loopback stand-in for the Microsoft identity platform, Microsoft Graph, Google's OAuth endpoint and the
Google Calendar API, for account tests. Never contacts a real service.

``browser(url)`` plays the user's browser: it reads the authorisation address Jarvis opened, issues a code
bound to its PKCE challenge and redirect address, and then visits that redirect address with the code and
the state, as the real sign-in page would. The token endpoint checks the code verifier, the client and
the redirect address, and refresh tokens can be rotated or revoked.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class FakeAccountsServer:
    def __init__(self):
        self.codes: Dict[str, Dict[str, str]] = {}
        self.refresh_tokens: Dict[str, Dict[str, str]] = {}
        self.access_tokens: Dict[str, str] = {}
        self.authorize_requests: List[Dict[str, str]] = []
        self.token_requests: List[Dict[str, str]] = []
        self.api_requests: List[str] = []
        self.rotate = True
        self.granted_scope: Optional[str] = None  # what the user agreed to, when not everything asked for
        self.user_mail = "owen@example.test"
        self.google_secret = "google-installed-secret"
        self.ms_events: List[Dict[str, Any]] = []
        self.inbox: List[Dict[str, Any]] = []
        self.google_calendars: List[Dict[str, Any]] = [{"id": self.user_mail, "primary": True, "selected": True}]
        self.google_events: Dict[str, List[Dict[str, Any]]] = {}
        self.page_size = 50
        self.redirect_result: Dict[str, Any] = {}
        self.fail_paths: set = set()  # API paths that answer 500
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
                if self.path.endswith("/token"):
                    status, body = owner._token(self.path, form)
                else:
                    status, body = 404, {}
                self._send(status, body)

            def do_GET(self):  # noqa: N802
                parts = urlsplit(self.path)
                owner.api_requests.append(parts.path)
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if token not in owner.access_tokens:
                    self._send(401, {"error": {"code": "InvalidAuthenticationToken"}})
                    return
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                status, body = owner._api(parts.path, query, dict(self.headers))
                self._send(status, body)

            def _send(self, status, body):
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── The user's browser ──────────────────────────────────────────────────

    def browser(self, url: str, *, state: Optional[str] = None, deny: bool = False, wait: bool = False) -> bool:
        """Sign in as the user would. ``state`` replaces the real state (a forged redirect); ``deny`` is the
        user pressing Cancel on the consent page. The redirect visit runs on its own thread, like a browser."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.authorize_requests.append(query)
        redirect = query["redirect_uri"]
        if deny:
            params = {"error": "access_denied", "state": query["state"]}
        else:
            code = secrets.token_hex(8)
            self.codes[code] = {"client_id": query["client_id"], "redirect_uri": redirect,
                                "challenge": query["code_challenge"], "scope": query["scope"]}
            params = {"code": code, "state": state if state is not None else query["state"]}
        target = f"{redirect}/?{urlencode(params)}"

        def visit():
            try:
                with urllib.request.urlopen(target.replace("://localhost:", "://127.0.0.1:"), timeout=10) as reply:
                    self.redirect_result = {"status": reply.status, "body": reply.read().decode("utf-8"),
                                            "headers": dict(reply.headers)}
            except Exception as exc:  # noqa: BLE001 - an HTTP error page is still a result
                body = exc.read().decode("utf-8") if hasattr(exc, "read") else ""
                self.redirect_result = {"status": getattr(exc, "code", 0), "body": body}

        thread = threading.Thread(target=visit, daemon=True)
        thread.start()
        if wait:
            thread.join(10)
        return True

    # ── Token endpoint ──────────────────────────────────────────────────────

    def _issue(self, client_id: str, scope: str) -> Dict[str, Any]:
        access, refresh = "at-" + secrets.token_hex(6), "rt-" + secrets.token_hex(6)
        self.access_tokens[access] = scope
        self.refresh_tokens[refresh] = {"client_id": client_id, "scope": scope}
        return {"access_token": access, "refresh_token": refresh, "expires_in": 3600, "scope": scope,
                "token_type": "Bearer"}

    def _token(self, path: str, form: Dict[str, str]):
        self.token_requests.append(dict(form, path=path))
        google = path.startswith("/google/")
        if google and form.get("client_secret") != self.google_secret:
            return 401, {"error": "invalid_client"}
        if form.get("grant_type") == "authorization_code":
            grant = self.codes.pop(form.get("code", ""), None)
            if (grant is None or grant["client_id"] != form.get("client_id")
                    or grant["redirect_uri"] != form.get("redirect_uri")
                    or _challenge(form.get("code_verifier", "")) != grant["challenge"]):
                return 400, {"error": "invalid_grant"}
            return 200, self._issue(form["client_id"], self.granted_scope or grant["scope"])
        if form.get("grant_type") == "refresh_token":
            held = self.refresh_tokens.get(form.get("refresh_token", ""))
            if held is None or held["client_id"] != form.get("client_id"):
                return 400, {"error": "invalid_grant", "error_description": "revoked"}
            issued = self._issue(held["client_id"], held["scope"])
            if self.rotate:
                del self.refresh_tokens[form["refresh_token"]]
            else:
                del self.refresh_tokens[issued["refresh_token"]]
                issued.pop("refresh_token")
            return 200, issued
        return 400, {"error": "unsupported_grant_type"}

    def revoke_all(self) -> None:
        """The user removed Jarvis's access in their account settings."""
        self.refresh_tokens.clear()
        self.access_tokens.clear()

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    # ── Microsoft Graph and Google Calendar ─────────────────────────────────

    def _page(self, items: List[Dict[str, Any]], offset: int, next_link) -> Dict[str, Any]:
        chunk = items[offset:offset + self.page_size]
        body: Dict[str, Any] = {"value": chunk}
        if offset + self.page_size < len(items):
            body.update(next_link(offset + self.page_size))
        return body

    def _api(self, path: str, query: Dict[str, str], headers: Dict[str, str]):
        if path in self.fail_paths:
            return 500, {"error": "server"}
        if path == "/graph/v1.0/me":
            return 200, {"mail": self.user_mail, "userPrincipalName": self.user_mail}
        if path == "/graph/v1.0/me/calendarView":
            offset = int(query.get("skip", 0))
            return 200, self._page(self.ms_events, offset, lambda n: {
                "@odata.nextLink": f"{self.base}/graph/v1.0/me/calendarView?skip={n}"})
        if path == "/graph/v1.0/me/mailFolders/inbox/messages":
            return 200, {"value": self.inbox}
        if path == "/calendar/v3/users/me/calendarList":
            return 200, {"items": self.google_calendars}
        if path.startswith("/calendar/v3/calendars/") and path.endswith("/events"):
            calendar = unquote(path[len("/calendar/v3/calendars/"):-len("/events")])
            if query.get("singleEvents") != "true":
                return 400, {"error": "recurring events must be expanded"}
            items = self.google_events.get(calendar, [])
            offset = int(query.get("pageToken") or 0)
            chunk = items[offset:offset + self.page_size]
            body: Dict[str, Any] = {"items": chunk}
            if offset + self.page_size < len(items):
                body["nextPageToken"] = str(offset + self.page_size)
            return 200, body
        return 404, {}
