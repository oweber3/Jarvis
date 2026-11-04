"""The web chat's loopback HTTP server: the built page's static files and a small JSON API.

Standard library only. Every request passes the Host and Origin checks first (``utils/local_guard``),
state-changing requests must be JSON, and nothing about a request (text, titles, names) is logged.
See ``webchat.spec.md``.
"""
from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from ..debug import debug_log
from ..utils.local_guard import asset_file, refusal
from . import identity
from .api import POLL_TIMEOUT_SEC, ChatApi, Refused as _Refused
from .uploads import MAX_UPLOAD_BYTES

MAX_BODY_BYTES = 64 * 1024
DRAIN_LIMIT_BYTES = 1024 * 1024

STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".png": "image/png",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Jarvis records, never the page, so the browser has nothing to ask permission for.
    "Permissions-Policy": "microphone=(), camera=(), geolocation=()",
}

@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    static_dir: Path = STATIC_DIR
    poll_timeout_sec: float = POLL_TIMEOUT_SEC


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        # Windows lets another socket of the same user bind a port that is already bound, and take its
        # connections, unless the first one asked for exclusive use.
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


class WebChatServer:
    """Serves the web chat for ``hub`` on the loopback interface."""

    def __init__(self, hub, settings: ServerSettings) -> None:
        self.hub = hub
        self.settings = settings
        # Known only to the daemon and the desktop app; the server proves it holds it (``/api/identity``).
        self.identity_token = identity.new_token()
        self._httpd: Optional[_HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd is not None else self.settings.port

    def start(self) -> None:
        """Bind and serve on a background thread. Raises ``OSError`` when the address cannot be bound."""
        self._httpd = _HTTPServer((self.settings.host, self.settings.port), _make_handler(self))
        self.hub.start()
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="jarvis-webchat-http", daemon=True)
        self._thread.start()
        debug_log(f"webchat: serving on port {self.port}", "webchat")

    def stop(self) -> None:
        self.hub.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        debug_log("webchat: stopped", "webchat")


def _make_handler(server: WebChatServer):
    api = ChatApi(server.hub, surface="web", origin="chat", poll_timeout_sec=server.settings.poll_timeout_sec,
                  identity_token=server.identity_token)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis"
        sys_version = ""
        # A client that stalls mid-request (an upload can be large) frees its thread and buffer after this.
        timeout = 60

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
            # Only an upload may be large; everything else is a small JSON body.
            limit = MAX_UPLOAD_BYTES if api.is_upload(self.command, urlsplit(self.path).path) else MAX_BODY_BYTES
            if length > limit:
                self._body_error = _Refused(413, "too_large")
                remaining = min(length, limit + DRAIN_LIMIT_BYTES)
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                self.close_connection = True
                return
            if length > 0:
                self._raw_body = self.rfile.read(length)

        def _upload_body(self) -> dict:
            """The raw bytes of an upload. What they are is decided from their content, never from a declared
            type or name, and no name or path is read from the request."""
            if self._body_error is not None:
                raise self._body_error
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/octet-stream":
                raise _Refused(415, "octet_stream_required")
            return {"bytes": self._raw_body}

        def _body(self) -> dict:
            if self._body_error is not None:
                raise self._body_error
            if not self._raw_body:
                return {}
            if not (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() == "application/json":
                raise _Refused(415, "json_required")
            try:
                data = json.loads(self._raw_body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise _Refused(400, "bad_json")
            if not isinstance(data, dict):
                raise _Refused(400, "bad_json")
            return data

        # -- dispatch ------------------------------------------------------------------

        def _guard(self) -> None:
            why = refusal(self.headers.get("Host", ""), self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site"))
            if why is not None:
                debug_log(f"webchat: refused a request with a foreign {why}", "webchat")
                raise _Refused(403, "forbidden")

        def _handle(self) -> None:
            self._guard()
            path = urlsplit(self.path).path
            if self.command in ("GET", "HEAD") and not path.startswith("/api/"):
                self._static(path)
                return
            if not path.startswith("/api/"):
                raise _Refused(404, "not_found")
            # Parsed first so a bad body is refused whatever the route.
            body = self._upload_body() if api.is_upload(self.command, path) else self._body()
            status, payload = api.dispatch(self.command, path, parse_qs(urlsplit(self.path).query), body)
            self._json(status, payload)

        def _run(self) -> None:
            try:
                self._read_raw_body()
                self._handle()
            except _Refused as refused:
                self._json(refused.status, {"error": refused.error})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception as exc:
                debug_log(f"webchat: request failed: {type(exc).__name__}", "webchat")
                try:
                    self._json(500, {"error": "internal"})
                except Exception:
                    pass

        do_GET = do_POST = do_PATCH = do_DELETE = do_HEAD = _run

        # -- static files ----------------------------------------------------------------

        def _static(self, path: str) -> None:
            root = Path(server.settings.static_dir)
            if path in ("/", "/index.html"):
                target = root / "index.html"
            elif path.startswith("/assets/"):
                target = asset_file(root / "assets", unquote(path[len("/assets/"):]))
                if target is None:
                    raise _Refused(404, "not_found")
            else:
                raise _Refused(404, "not_found")
            if not target.is_file():
                raise _Refused(404, "not_found")
            content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, target.read_bytes(), content_type, api=False)

    return Handler
