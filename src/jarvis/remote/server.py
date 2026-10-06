"""The phone access web server: the phone app's static files and a small token-protected JSON API.

Standard library only. Clients outside private networks are refused before anything else, every API call
except pairing needs a paired device's token, and nothing about a request (token, text, name) is logged.
See ``remote.spec.md``.
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import parse_qs, urlsplit

from ..debug import debug_log
from .networks import is_allowed_client

MAX_BODY_BYTES = 16 * 1024
DRAIN_LIMIT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 4000
POLL_TIMEOUT_SEC = 25.0

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    quick_actions: List[str] = field(default_factory=list)
    allow_confirm: bool = True
    poll_timeout_sec: float = POLL_TIMEOUT_SEC


class _Refused(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status, self.error = status, error


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _HTTPServerV6(_HTTPServer):
    address_family = socket.AF_INET6


class RemoteServer:
    """Serves the phone app for ``hub`` with devices from ``store``."""

    def __init__(self, hub, store, settings: ServerSettings, *,
                 client_filter: Callable[[str], bool] = is_allowed_client) -> None:
        self.hub = hub
        self.store = store
        self.settings = settings
        self._client_filter = client_filter
        self._httpd: Optional[_HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd is not None else self.settings.port

    def start(self) -> None:
        """Bind and serve on a background thread. Raises ``OSError`` when the address cannot be bound."""
        server_class = _HTTPServerV6 if ":" in self.settings.host else _HTTPServer
        self._httpd = server_class((self.settings.host, self.settings.port), _make_handler(self))
        self.hub.start()
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="jarvis-remote-http", daemon=True)
        self._thread.start()
        debug_log(f"remote: serving on port {self.port}", "remote")

    def stop(self) -> None:
        self.hub.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        debug_log("remote: stopped", "remote")


def _int_param(query: dict, name: str, default: int) -> int:
    try:
        return int(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


def _make_handler(server: RemoteServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis"
        sys_version = ""

        def log_message(self, format, *args) -> None:  # noqa: A002 - request lines never reach the console
            return

        # -- responses -------------------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str, *, api: bool) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if api else "no-cache")
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json", api=True)

        # -- request parts -----------------------------------------------------------

        def _read_raw_body(self) -> None:
            """Read the whole body up front: answering with unread data in the socket resets the
            connection on Windows, and the client would never see the status."""
            self._raw_body, self._body_error = b"", None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._body_error = _Refused(400, "bad_length")
                self.close_connection = True
                return
            if length > MAX_BODY_BYTES:
                self._body_error = _Refused(413, "too_large")
                remaining = min(length, DRAIN_LIMIT_BYTES)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self.close_connection = True
                return
            if length > 0:
                self._raw_body = self.rfile.read(length)

        def _body(self) -> dict:
            if self._body_error is not None:
                raise self._body_error
            if not self._raw_body:
                return {}
            try:
                data = json.loads(self._raw_body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise _Refused(400, "bad_json")
            if not isinstance(data, dict):
                raise _Refused(400, "bad_json")
            return data

        def _device(self):
            header = self.headers.get("Authorization") or ""
            token = header[7:].strip() if header.startswith("Bearer ") else ""
            device = server.store.authenticate(token)
            if device is None:
                if token:
                    debug_log("remote: unknown device token refused", "remote")
                raise _Refused(401, "unpaired")
            return device

        # -- dispatch ------------------------------------------------------------------

        def _handle(self) -> None:
            if not server._client_filter(self.client_address[0]):
                debug_log("remote: request from outside private networks refused", "remote")
                raise _Refused(403, "forbidden")
            path = urlsplit(self.path).path
            if self.command in ("GET", "HEAD") and path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                self._send(200, (STATIC_DIR / name).read_bytes(), content_type, api=False)
                return
            if not path.startswith("/api/"):
                raise _Refused(404, "not_found")
            if self.command == "POST" and path == "/api/pair":
                self._pair()
                return
            device = self._device()
            route = {
                ("GET", "/api/poll"): self._poll,
                ("POST", "/api/chat"): self._chat,
                ("POST", "/api/stop"): self._stop,
                ("POST", "/api/confirm"): self._confirm,
                ("POST", "/api/unpair"): lambda: self._unpair(device),
            }.get((self.command, path))
            if route is None:
                raise _Refused(404, "not_found")
            route()

        def _run(self) -> None:
            try:
                self._read_raw_body()
                self._handle()
            except _Refused as refused:
                self._json(refused.status, {"error": refused.error})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as exc:
                debug_log(f"remote: request failed: {type(exc).__name__}", "remote")
                try:
                    self._json(500, {"error": "internal"})
                except Exception:
                    pass

        do_GET = do_POST = do_HEAD = _run

        # -- endpoints -------------------------------------------------------------------

        def _pair(self) -> None:
            body = self._body()
            result = server.store.complete_pairing(body.get("code"), body.get("name"))
            if result is None:
                raise _Refused(403, "bad_code")
            self._json(200, {"token": result.token, "device_id": result.device_id})

        def _poll(self) -> None:
            query = parse_qs(urlsplit(self.path).query)
            snapshot = server.hub.wait(rev=_int_param(query, "rev", -1), after=_int_param(query, "after", 0),
                                       timeout_sec=server.settings.poll_timeout_sec)
            if not server.settings.allow_confirm:
                snapshot["confirmation"] = None
            snapshot["quick_actions"] = list(server.settings.quick_actions)
            snapshot["allow_confirm"] = server.settings.allow_confirm
            self._json(200, snapshot)

        def _chat(self) -> None:
            text = self._body().get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                raise _Refused(400, "bad_text")
            result = server.hub.submit(text)
            status = {"accepted": 202, "busy": 409}.get(result.status, 503)
            self._json(status, {"query_id": result.query_id, "status": result.status})

        def _stop(self) -> None:
            server.hub.cancel()
            self._json(200, {"ok": True})

        def _confirm(self) -> None:
            if not server.settings.allow_confirm:
                raise _Refused(403, "confirm_disabled")
            body = self._body()
            request_id, approve = body.get("id"), body.get("approve")
            if not isinstance(request_id, str) or not isinstance(approve, bool):
                raise _Refused(400, "bad_answer")
            if not server.hub.resolve_confirmation(request_id, approve):
                raise _Refused(409, "not_pending")
            self._json(200, {"ok": True})

        def _unpair(self, device) -> None:
            server.store.revoke(device.id)
            self._json(200, {"ok": True})

    return Handler
