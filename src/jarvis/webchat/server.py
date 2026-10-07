"""The web chat's loopback HTTP server: the built page's static files and a small JSON API.

Standard library only. Every request passes the Host and Origin checks first (``utils/local_guard``),
state-changing requests must be JSON, and nothing about a request (text, titles, names) is logged.
See ``webchat.spec.md``.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from ..debug import debug_log
from ..utils.local_guard import refusal
from .hub import chat_dict

MAX_BODY_BYTES = 64 * 1024
DRAIN_LIMIT_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 4000
POLL_TIMEOUT_SEC = 25.0

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
}

_PROJECT_PATH = re.compile(r"^/api/projects/([0-9a-f]+)$")
_CHAT_PATH = re.compile(r"^/api/chats/([0-9a-f]+)$")
_CHAT_OPEN_PATH = re.compile(r"^/api/chats/([0-9a-f]+)/open$")


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    static_dir: Path = STATIC_DIR
    poll_timeout_sec: float = POLL_TIMEOUT_SEC


class _Refused(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status, self.error = status, error


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class WebChatServer:
    """Serves the web chat for ``hub`` on the loopback interface."""

    def __init__(self, hub, settings: ServerSettings) -> None:
        self.hub = hub
        self.settings = settings
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


def _int_param(query: dict, name: str, default: int) -> int:
    try:
        return int(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


def _make_handler(server: WebChatServer):
    hub = server.hub
    store = hub.store

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
            why = refusal(self.headers.get("Host", ""), self.headers.get("Origin"))
            if why is None and self.headers.get("Sec-Fetch-Site") == "cross-site":
                why = "origin"
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
            body = self._body()  # parsed first so a bad body is refused whatever the route
            handler = self._route(path)
            handler(body)

        def _route(self, path: str):
            method = self.command
            if (method, path) in self._FIXED:
                return getattr(self, self._FIXED[(method, path)])
            for pattern, table in ((_PROJECT_PATH, self._PROJECT), (_CHAT_PATH, self._CHAT),
                                   (_CHAT_OPEN_PATH, self._OPEN)):
                found = pattern.match(path)
                if found and method in table:
                    identifier = found.group(1)
                    handler = getattr(self, table[method])
                    return lambda body: handler(identifier, body)
            raise _Refused(404, "not_found")

        _FIXED = {
            ("GET", "/api/state"): "_state",
            ("GET", "/api/models"): "_models",
            ("GET", "/api/library"): "_library",
            ("GET", "/api/poll"): "_poll",
            ("POST", "/api/projects"): "_create_project",
            ("POST", "/api/chats"): "_create_chat",
            ("POST", "/api/chat"): "_chat",
            ("POST", "/api/stop"): "_stop",
            ("POST", "/api/model"): "_model",
            ("POST", "/api/clear"): "_clear",
        }
        _PROJECT = {"PATCH": "_rename_project", "DELETE": "_delete_project"}
        _CHAT = {"GET": "_get_chat", "PATCH": "_patch_chat", "DELETE": "_delete_chat"}
        _OPEN = {"POST": "_open_chat"}

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
                name = unquote(path[len("/assets/"):])
                assets = (root / "assets").resolve()
                target = (assets / name).resolve()
                if not name or assets not in target.parents:
                    raise _Refused(404, "not_found")
            else:
                raise _Refused(404, "not_found")
            if not target.is_file():
                raise _Refused(404, "not_found")
            content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
            self._send(200, target.read_bytes(), content_type, api=False)

        # -- endpoints: state ------------------------------------------------------------

        def _state(self, _body) -> None:
            snapshot = hub.snapshot(2 ** 62)
            self._json(200, {key: snapshot[key] for key in ("ready", "state", "busy", "active_chat_id", "mode", "model")})

        def _models(self, _body) -> None:
            self._json(200, hub.models())

        def _library(self, _body) -> None:
            self._json(200, hub.library())

        def _poll(self, _body) -> None:
            query = parse_qs(urlsplit(self.path).query)
            self._json(200, hub.wait(rev=_int_param(query, "rev", -1), after=_int_param(query, "after", 0),
                                     timeout_sec=server.settings.poll_timeout_sec))

        # -- endpoints: projects ---------------------------------------------------------

        @staticmethod
        def _name(body: dict, key: str = "name") -> str:
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                raise _Refused(400, "bad_name")
            return value

        def _create_project(self, body) -> None:
            project = store.create_project(self._name(body))
            hub.changed_library()
            self._json(201, {"id": project.id, "name": project.name, "position": project.position})

        def _rename_project(self, project_id, body) -> None:
            if not store.rename_project(project_id, self._name(body)):
                raise _Refused(404, "unknown")
            hub.changed_library()
            self._json(200, {"ok": True})

        def _delete_project(self, project_id, _body) -> None:
            if not store.delete_project(project_id):
                raise _Refused(404, "unknown")
            hub.changed_library()
            self._json(200, {"ok": True})

        # -- endpoints: chats --------------------------------------------------------------

        @staticmethod
        def _answer_for(status: str) -> int:
            return {"ok": 200, "unknown": 404, "busy": 409, "unavailable": 503}[status]

        def _create_chat(self, body) -> None:
            project_id = body.get("project_id")
            if project_id is not None and not isinstance(project_id, str):
                raise _Refused(400, "bad_project")
            result = hub.new_chat(project_id)
            if result.status != "ok":
                raise _Refused(self._answer_for(result.status), result.status)
            self._json(201, {"chat": chat_dict(result.chat)})

        def _open_chat(self, chat_id, _body) -> None:
            result = hub.open_chat(chat_id)
            if result.status != "ok":
                raise _Refused(self._answer_for(result.status), result.status)
            self._json(200, {"chat": chat_dict(result.chat)})

        def _get_chat(self, chat_id, _body) -> None:
            chat = store.get_chat(chat_id)
            if chat is None:
                raise _Refused(404, "unknown")
            self._json(200, {"chat": chat_dict(chat),
                             "messages": [{"id": m.id, "role": m.role, "text": m.content, "ts": m.ts,
                                           "source": m.source} for m in store.messages(chat_id)]})

        def _patch_chat(self, chat_id, body) -> None:
            if store.get_chat(chat_id) is None:
                raise _Refused(404, "unknown")
            if "title" in body:
                title = body["title"]
                if not isinstance(title, str) or not title.strip():
                    raise _Refused(400, "bad_name")
            if "project_id" in body and body["project_id"] is not None and not isinstance(body["project_id"], str):
                raise _Refused(400, "bad_project")
            if "project_id" in body and not store.move_chat(chat_id, body["project_id"]):
                raise _Refused(404, "unknown_project")
            if "title" in body:
                store.rename_chat(chat_id, body["title"])
            hub.changed_library()
            self._json(200, {"ok": True})

        def _delete_chat(self, chat_id, _body) -> None:
            status = hub.delete_chat(chat_id)
            if status != "ok":
                raise _Refused(self._answer_for(status), status)
            self._json(200, {"ok": True})

        def _clear(self, body) -> None:
            if body.get("confirm") is not True:
                raise _Refused(400, "confirm_required")
            status = hub.delete_all()
            if status != "ok":
                raise _Refused(self._answer_for(status), status)
            self._json(200, {"ok": True})

        # -- endpoints: requests and models -----------------------------------------------

        def _chat(self, body) -> None:
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                raise _Refused(400, "bad_text")
            result = hub.submit(text)
            status = {"accepted": 202, "busy": 409}.get(result.status, 503)
            self._json(status, {"query_id": result.query_id, "status": result.status})

        def _stop(self, _body) -> None:
            hub.cancel()
            self._json(200, {"ok": True})

        def _model(self, body) -> None:
            kind, value = body.get("kind"), body.get("value")
            if kind not in ("mode", "local") or not isinstance(value, str) or not value.strip():
                raise _Refused(400, "bad_model")
            result = hub.switch_mode(value) if kind == "mode" else hub.set_local_model(value)
            if not result.ok:
                raise _Refused(409, result.reason or "refused")
            self._json(200, {"ok": True})

    return Handler
