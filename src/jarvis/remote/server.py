"""The phone access web server: the phone app's static files and a token-protected JSON API, over HTTP or HTTPS.

Standard library only. Clients outside private networks are refused before anything else, every API call
except pairing needs a paired device's token, and nothing about a request (token, text, name, audio) is
logged. The chat routes are the web chat's (``webchat/api.py``); this adds pairing, confirmations, unpairing,
the spoken request and the reply audio. See ``remote.spec.md``.
"""
from __future__ import annotations

import json
import re
import select
import socket
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from ..debug import debug_log
from ..utils.local_guard import asset_file, refusal
from ..webchat.api import POLL_TIMEOUT_SEC, ChatApi, Refused as _Refused
from ..webchat.uploads import MAX_UPLOAD_BYTES
from .networks import is_allowed_client, is_own_host, pc_name
from .phone import PhoneState
from .tls import ReloadingContext, TLSConfig
from .voice import MAX_VOICE_BYTES, PhoneVoice

MAX_BODY_BYTES = 64 * 1024
DRAIN_LIMIT_BYTES = 1024 * 1024
HANDSHAKE_TIMEOUT_SEC = 10.0

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/pcm-worklet.js": ("pcm-worklet.js", "text/javascript; charset=utf-8"),
}
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
    ".woff2": "font/woff2",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Only the phone's own page may ask for the microphone (voice); nothing may ask for the camera or location.
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
}

_AUDIO_PATH = re.compile(r"^/api/audio/([0-9a-f]{32})$")
_OCTET_ROUTES = {("POST", "/api/voice"), ("POST", "/api/attachments")}


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    quick_actions: List[str] = field(default_factory=list)
    allow_confirm: bool = True
    poll_timeout_sec: float = POLL_TIMEOUT_SEC
    tls: Optional[TLSConfig] = None
    tls_host: str = ""
    static_dir: Path = STATIC_DIR


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    tls: Optional[ReloadingContext] = None

    def get_request(self):
        sock, address = super().get_request()
        if self.tls is not None:
            # Wrapped here, handshaken on the connection's own thread (the handler's setup), so a client that
            # stalls in the handshake never blocks the accept loop.
            sock = self.tls.context().wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def handle_error(self, request, client_address) -> None:
        # A failed handshake or a dropped connection is not worth a traceback.
        debug_log("remote: a connection failed before its request", "remote")


class _HTTPServerV6(_HTTPServer):
    address_family = socket.AF_INET6


class RemoteServer:
    """Serves the phone app for ``hub`` (the shared ``ChatHub``) with devices from ``store``.

    ``backend`` is the remote ``DaemonBackend`` (or a fake): confirmations, statistics and speech."""

    def __init__(self, hub, store, backend, settings: ServerSettings, *,
                 client_filter: Callable[[str], bool] = is_allowed_client) -> None:
        self.hub = hub
        self.store = store
        self.settings = settings
        self._client_filter = client_filter
        own_name = pc_name()
        extra = (settings.tls_host.lower(),) if settings.tls_host else ()
        self._host_allowed: Callable[[str], bool] = lambda name: is_own_host(name, own_name, extra)
        self.secure = settings.tls is not None
        self.voice = PhoneVoice(backend, hub)
        self.state = PhoneState(backend, self.voice, hub, quick_actions=settings.quick_actions,
                                allow_confirm=settings.allow_confirm, secure=self.secure)
        self.api = ChatApi(hub, surface="phone", origin="phone", poll_timeout_sec=settings.poll_timeout_sec,
                           snapshot_extra=self.state.snapshot, voice=self.voice)
        self._httpd: Optional[_HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd is not None else self.settings.port

    def start(self) -> None:
        """Bind and serve on a background thread. Raises ``OSError`` when the address cannot be bound and
        ``TLSError`` when the certificate cannot be used."""
        tls = ReloadingContext(self.settings.tls.cert, self.settings.tls.key) if self.settings.tls else None
        server_class = _HTTPServerV6 if ":" in self.settings.host else _HTTPServer
        httpd = server_class((self.settings.host, self.settings.port), _make_handler(self))
        httpd.tls = tls
        self._httpd = httpd
        self.hub.start()
        self.hub.watch(self.state.signal)
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="jarvis-remote-http", daemon=True)
        self._thread.start()
        debug_log(f"remote: serving {'HTTPS' if tls else 'HTTP'} on port {self.port}", "remote")

    def stop(self) -> None:
        self.hub.unwatch(self.state.signal)
        self.voice.shutdown()
        self.hub.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        debug_log("remote: stopped", "remote")


def _declares(content_type: Optional[str], expected: str) -> bool:
    """A page can only send ``application/json`` or ``application/octet-stream`` to another origin after a CORS
    preflight this server never answers, so such a body cannot come from a plain form post on another site."""
    return (content_type or "").split(";")[0].strip().lower() == expected


def _make_handler(server: RemoteServer):
    api = server.api
    tls = server.secure

    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis"
        sys_version = ""
        # A client that stalls mid-request (an upload can be large) frees its thread and buffer after this.
        timeout = 60

        def setup(self) -> None:
            if isinstance(self.request, ssl.SSLSocket):
                self.request.settimeout(HANDSHAKE_TIMEOUT_SEC)
                self.request.do_handshake()
            super().setup()

        def log_message(self, format, *args) -> None:  # noqa: A002 - request lines never reach the console
            return

        # -- responses -------------------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str, *, api_response: bool) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if api_response else "no-cache")
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json", api_response=True)

        # -- request parts -----------------------------------------------------------

        def _limit(self, path: str) -> int:
            if (self.command, path) == ("POST", "/api/voice"):
                return MAX_VOICE_BYTES
            if api.is_upload(self.command, path):
                return MAX_UPLOAD_BYTES
            return MAX_BODY_BYTES

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
            limit = self._limit(urlsplit(self.path).path)
            if limit > MAX_BODY_BYTES and length > MAX_BODY_BYTES and not self._may_send_large():
                # Only a paired phone on a private network may make the server hold megabytes.
                self._body_error = _Refused(401, "unpaired")
                self.close_connection = True
                return
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

        def _may_send_large(self) -> bool:
            if not server._client_filter(self.client_address[0]):
                return False
            header = self.headers.get("Authorization") or ""
            token = header[7:].strip() if header.startswith("Bearer ") else ""
            return server.store.authenticate(token) is not None

        def _client_gone(self) -> bool:
            """Whether the phone has hung up (an abandoned spoken request is not run). Reads nothing the request
            still needs: the whole body was read before the route ran."""
            connection = self.connection
            try:
                if not select.select([connection], [], [], 0)[0]:
                    return False
                if isinstance(connection, ssl.SSLSocket):
                    return connection.recv(1) == b""
                return connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

        def _body(self) -> dict:
            if self._body_error is not None:
                raise self._body_error
            if not self._raw_body:
                return {}
            if not _declares(self.headers.get("Content-Type"), "application/json"):
                raise _Refused(415, "json_required")
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
            is_api = path.startswith("/api/")
            # A link from another site may open the app; only the API refuses cross-site fetches.
            why = refusal(self.headers.get("Host", ""), self.headers.get("Origin"),
                          self.headers.get("Sec-Fetch-Site") if is_api else None, host_allowed=server._host_allowed)
            if why is not None:
                debug_log(f"remote: refused a request with a foreign {why}", "remote")
                raise _Refused(403, "forbidden")
            octet = (self.command, path) in _OCTET_ROUTES
            declared = self.headers.get("Content-Type")
            if self.command == "POST" and octet and self._raw_body and not _declares(declared, "application/octet-stream"):
                raise _Refused(415, "octet_stream_required")
            if self.command == "POST" and not octet and (self._raw_body or path == "/api/pair") \
                    and not _declares(declared, "application/json"):
                raise _Refused(415, "json_required")
            if self.command in ("GET", "HEAD") and not is_api:
                self._static(path)
                return
            if not is_api:
                raise _Refused(404, "not_found")
            if self.command == "POST" and path == "/api/pair":
                self._pair()
                return
            device = self._device()
            if self._phone_route(device, path):
                return
            body = {"bytes": self._raw_body} if api.is_upload(self.command, path) else self._body()
            if self._body_error is not None:
                raise self._body_error
            status, payload = api.dispatch(self.command, path, parse_qs(urlsplit(self.path).query), body, device.id)
            self._json(status, payload)

        def _phone_route(self, device, path: str) -> bool:
            """The routes only a phone has. True when one answered."""
            command = self.command
            if (command, path) == ("POST", "/api/confirm"):
                self._confirm()
            elif (command, path) == ("POST", "/api/unpair"):
                server.store.revoke(device.id)
                self._json(200, {"ok": True})
            elif (command, path) == ("POST", "/api/voice"):
                if self._body_error is not None:
                    raise self._body_error
                status, payload = server.voice.handle_audio(device.id, self._raw_body, secure=tls,
                                                            alive=lambda: not self._client_gone())
                self._json(status, payload)
            elif (command, path) == ("POST", "/api/stop"):
                server.voice.cancel()   # a spoken request still being transcribed is dropped too
                return False
            elif command == "GET" and _AUDIO_PATH.match(path):
                clip = server.voice.fetch(_AUDIO_PATH.match(path).group(1), device.id)
                if clip is None:
                    raise _Refused(404, "not_found")
                self._send(200, clip, "audio/wav", api_response=True)
            else:
                return False
            return True

        def _run(self) -> None:
            try:
                self._read_raw_body()
                self._handle()
            except _Refused as refused:
                self._json(refused.status, {"error": refused.error})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ssl.SSLError):
                pass
            except Exception as exc:
                debug_log(f"remote: request failed: {type(exc).__name__}", "remote")
                try:
                    self._json(500, {"error": "internal"})
                except Exception:
                    pass

        do_GET = do_POST = do_PATCH = do_DELETE = do_HEAD = _run

        # -- static files ----------------------------------------------------------------

        def _static(self, path: str) -> None:
            root = Path(server.settings.static_dir)
            if path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                target = root / name
            elif path.startswith("/assets/"):
                target = asset_file(root / "assets", unquote(path[len("/assets/"):]))
                if target is None:
                    raise _Refused(404, "not_found")
                content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            else:
                raise _Refused(404, "not_found")
            if not target.is_file():
                raise _Refused(404, "not_found")
            self._send(200, target.read_bytes(), content_type, api_response=False)

        # -- endpoints -------------------------------------------------------------------

        def _pair(self) -> None:
            body = self._body()
            result = server.store.complete_pairing(body.get("code"), body.get("name"))
            if result is None:
                raise _Refused(403, "bad_code")
            self._json(200, {"token": result.token, "device_id": result.device_id})

        def _confirm(self) -> None:
            if not server.state.allow_confirm:
                raise _Refused(403, "confirm_disabled")
            body = self._body()
            request_id, approve = body.get("id"), body.get("approve")
            if not isinstance(request_id, str) or not isinstance(approve, bool):
                raise _Refused(400, "bad_answer")
            if not server.state.answer(request_id, approve):
                raise _Refused(409, "not_pending")
            self._json(200, {"ok": True})

    return Handler
