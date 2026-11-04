"""The chat HTTP API as functions of method, path, query and body, shared by the web chat's server and phone access.

Neither server's guards, authentication or transport live here: a server decides who may call, reads the
request, and passes the parts to ``ChatApi.dispatch``, which answers with a status and a JSON-able payload (or
raises ``Refused``). Two surfaces exist: ``web`` (every route) and ``phone`` (without the routes that act on the
PC itself). See ``webchat.spec.md``, HTTP API and Two surfaces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..memory.chat_store import SEARCH_QUERY_MAX_CHARS
from . import identity
from .hub import chat_dict, message_dict
from .mic import MAX_RECORDING_SEC
from .uploads import UploadRefused

MAX_TEXT_CHARS = 4000
POLL_TIMEOUT_SEC = 25.0
# A long chat is read a page at a time (``GET /api/chats/<id>?limit=``); the page asks for no more than this.
MAX_PAGE_MESSAGES = 200
DEFAULT_PAGE_MESSAGES = 50

_PROJECT_PATH = re.compile(r"^/api/projects/([0-9a-f]+)$")
_CHAT_PATH = re.compile(r"^/api/chats/([0-9a-f]+)$")
_CHAT_OPEN_PATH = re.compile(r"^/api/chats/([0-9a-f]+)/open$")
_SESSION = re.compile(r"^[0-9a-f]{32}$")
_UPLOAD_PATH = re.compile(r"^/api/attachments/([0-9a-f]+)$")
UPLOAD_ROUTE = ("POST", "/api/attachments")

# Routes that act on the PC itself (its microphone, its screen, its history as a whole) or prove the web
# chat's own identity; the phone surface answers them as unknown.
WEB_ONLY = frozenset({
    ("POST", "/api/clear"), ("POST", "/api/attachments/screenshot"), ("GET", "/api/identity"),
    ("GET", "/api/mic"), ("POST", "/api/mic/start"), ("POST", "/api/mic/heartbeat"), ("POST", "/api/mic/stop"),
    ("POST", "/api/mic/cancel"),
})


class Refused(Exception):
    """A request the API (or its server) turned away: an HTTP status and an error code."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status, self.error = status, error


@dataclass(frozen=True)
class Call:
    """What a route sees: the parsed body (an upload is ``{"bytes": ...}``), the query string, and whatever
    the server knows about the caller (``context``, a paired phone's device id)."""
    body: Dict[str, Any]
    query: Dict[str, List[str]] = field(default_factory=dict)
    context: Any = None


def _int_param(query: dict, name: str, default: int) -> int:
    try:
        return int(query.get(name, [default])[0])
    except (TypeError, ValueError):
        return default


class ChatApi:
    """Routes for ``hub`` (a ``ChatHub``). ``origin`` is what the daemon is told a request came from.

    ``snapshot_extra(context)`` adds a ``phone`` object to every poll; ``voice`` (phone access's
    ``PhoneVoice``) makes ``POST /api/chat`` honour ``"speak": true``. ``identity_token`` backs the web
    chat's ``/api/identity``."""

    def __init__(self, hub, *, surface: str = "web", origin: str = "chat",
                 poll_timeout_sec: float = POLL_TIMEOUT_SEC, identity_token: Optional[str] = None,
                 snapshot_extra: Optional[Callable[[Any], Dict[str, Any]]] = None, voice=None) -> None:
        if surface not in ("web", "phone"):
            raise ValueError(surface)
        self.hub = hub
        self.store = hub.store
        self.surface = surface
        self.origin = origin
        self.poll_timeout_sec = poll_timeout_sec
        self.identity_token = identity_token
        self.snapshot_extra = snapshot_extra
        self.voice = voice

    # -- dispatch ------------------------------------------------------------------

    @staticmethod
    def is_upload(method: str, path: str) -> bool:
        return (method, path) == UPLOAD_ROUTE

    def dispatch(self, method: str, path: str, query: Dict[str, List[str]], body: Dict[str, Any],
                 context: Any = None) -> Tuple[int, Any]:
        """Run the route for ``method`` and ``path``: ``(status, payload)``. Raises ``Refused``."""
        handler = self._route(method, path)
        return handler(Call(body, query, context))

    def _route(self, method: str, path: str):
        if (method, path) in self._FIXED and (self.surface == "web" or (method, path) not in WEB_ONLY):
            return getattr(self, self._FIXED[(method, path)])
        for pattern, table in ((_PROJECT_PATH, self._PROJECT), (_CHAT_PATH, self._CHAT),
                               (_CHAT_OPEN_PATH, self._OPEN), (_UPLOAD_PATH, self._UPLOAD)):
            found = pattern.match(path)
            if found and method in table:
                identifier = found.group(1)
                handler = getattr(self, table[method])
                return lambda call: handler(identifier, call)
        raise Refused(404, "not_found")

    _FIXED = {
        ("GET", "/api/state"): "_state",
        ("GET", "/api/models"): "_models",
        ("GET", "/api/library"): "_library",
        ("GET", "/api/suggestions"): "_suggestions",
        ("GET", "/api/search"): "_search",
        ("GET", "/api/poll"): "_poll",
        ("GET", "/api/identity"): "_identity",
        ("POST", "/api/projects"): "_create_project",
        ("POST", "/api/chats"): "_create_chat",
        ("POST", "/api/chat"): "_chat",
        ("GET", "/api/attachments/support"): "_attachment_support",
        ("POST", "/api/attachments"): "_upload",
        ("POST", "/api/attachments/screenshot"): "_screenshot",
        ("POST", "/api/stop"): "_stop",
        ("POST", "/api/model"): "_model",
        ("POST", "/api/clear"): "_clear",
        ("GET", "/api/mic"): "_mic_status",
        ("POST", "/api/mic/start"): "_mic_start",
        ("POST", "/api/mic/heartbeat"): "_mic_heartbeat",
        ("POST", "/api/mic/stop"): "_mic_stop",
        ("POST", "/api/mic/cancel"): "_mic_cancel",
    }
    _PROJECT = {"PATCH": "_rename_project", "DELETE": "_delete_project"}
    _CHAT = {"GET": "_get_chat", "PATCH": "_patch_chat", "DELETE": "_delete_chat"}
    _OPEN = {"POST": "_open_chat"}
    _UPLOAD = {"DELETE": "_discard_upload"}

    # -- state ---------------------------------------------------------------------

    def _state(self, _call):
        snapshot = self.hub.snapshot(2 ** 62)
        return 200, {key: snapshot[key] for key in ("ready", "state", "busy", "active_chat_id", "mode", "model", "cloud")}

    def _models(self, _call):
        return 200, self.hub.models()

    def _library(self, _call):
        return 200, self.hub.library()

    def _suggestions(self, _call):
        return 200, self.hub.suggestions()

    def _search(self, call):
        query = call.query.get("q", [""])[0]
        if not query.strip() or len(query) > SEARCH_QUERY_MAX_CHARS:
            raise Refused(400, "bad_query")
        return 200, {"results": [{"chat_id": h.chat_id, "message_id": h.message_id, "snippet": h.snippet, "ts": h.ts}
                                 for h in self.store.search_messages(query)]}

    def _poll(self, call):
        snapshot = self.hub.wait(rev=_int_param(call.query, "rev", -1), after=_int_param(call.query, "after", 0),
                                 timeout_sec=self.poll_timeout_sec)
        if self.snapshot_extra is not None:
            snapshot["phone"] = self.snapshot_extra(call.context)
        return 200, snapshot

    def _identity(self, call):
        nonce = call.query.get("nonce", [""])[0]
        if not identity.is_valid_nonce(nonce):
            raise Refused(400, "bad_nonce")
        return 200, {"proof": identity.proof(self.identity_token, nonce)}

    # -- projects ------------------------------------------------------------------

    @staticmethod
    def _name(body: dict, key: str = "name") -> str:
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            raise Refused(400, "bad_name")
        return value

    def _create_project(self, call):
        project = self.store.create_project(self._name(call.body))
        self.hub.changed_library()
        return 201, {"id": project.id, "name": project.name, "position": project.position}

    def _rename_project(self, project_id, call):
        if not self.store.rename_project(project_id, self._name(call.body)):
            raise Refused(404, "unknown")
        self.hub.changed_library()
        return 200, {"ok": True}

    def _delete_project(self, project_id, _call):
        if not self.store.delete_project(project_id):
            raise Refused(404, "unknown")
        self.hub.changed_library()
        return 200, {"ok": True}

    # -- chats ---------------------------------------------------------------------

    @staticmethod
    def _answer_for(status: str) -> int:
        return {"ok": 200, "unknown": 404, "busy": 409, "unavailable": 503}[status]

    def _create_chat(self, call):
        project_id = call.body.get("project_id")
        if project_id is not None and not isinstance(project_id, str):
            raise Refused(400, "bad_project")
        result = self.hub.new_chat(project_id)
        if result.status != "ok":
            raise Refused(self._answer_for(result.status), result.status)
        return 201, {"chat": chat_dict(result.chat)}

    def _open_chat(self, chat_id, _call):
        result = self.hub.open_chat(chat_id)
        if result.status != "ok":
            raise Refused(self._answer_for(result.status), result.status)
        return 200, {"chat": chat_dict(result.chat)}

    @staticmethod
    def _page(query: dict) -> Optional[dict]:
        """The page of a chat a request asks for: ``limit`` and one of ``before`` or ``around`` (message ids), or
        None for the whole chat. Raises ``Refused`` for anything else."""
        names = [name for name in ("limit", "before", "around") if name in query]
        if not names:
            return None
        try:
            values = {name: int(query[name][0]) for name in names}
        except (TypeError, ValueError):
            raise Refused(400, "bad_page")
        limit = values.get("limit", DEFAULT_PAGE_MESSAGES)
        if not 1 <= limit <= MAX_PAGE_MESSAGES or ("before" in values and "around" in values):
            raise Refused(400, "bad_page")
        return {"limit": limit, "before": values.get("before"), "around": values.get("around")}

    def _get_chat(self, chat_id, call):
        chat = self.store.get_chat(chat_id)
        if chat is None:
            raise Refused(404, "unknown")
        page = self._page(call.query)
        if page is None:
            found = self.store.messages(chat_id)
            earlier = later = False
        else:
            window = self.store.messages_page(chat_id, page["limit"], before=page["before"], around=page["around"])
            found, earlier, later = window.messages, window.has_earlier, window.has_later
        return 200, {"chat": chat_dict(chat), "messages": [message_dict(m) for m in found],
                     "has_earlier": earlier, "has_later": later}

    def _patch_chat(self, chat_id, call):
        body = call.body
        if self.store.get_chat(chat_id) is None:
            raise Refused(404, "unknown")
        if "title" in body:
            title = body["title"]
            if not isinstance(title, str) or not title.strip():
                raise Refused(400, "bad_name")
        if "project_id" in body and body["project_id"] is not None and not isinstance(body["project_id"], str):
            raise Refused(400, "bad_project")
        if "pinned" in body and not isinstance(body["pinned"], bool):
            raise Refused(400, "bad_pinned")
        if "project_id" in body and not self.store.move_chat(chat_id, body["project_id"]):
            raise Refused(404, "unknown_project")
        if "title" in body:
            self.store.rename_chat(chat_id, body["title"])
        if "pinned" in body:
            self.store.set_pinned(chat_id, body["pinned"])
        self.hub.changed_library()
        return 200, {"ok": True}

    def _delete_chat(self, chat_id, _call):
        status = self.hub.delete_chat(chat_id)
        if status != "ok":
            raise Refused(self._answer_for(status), status)
        return 200, {"ok": True}

    def _clear(self, call):
        if call.body.get("confirm") is not True:
            raise Refused(400, "confirm_required")
        status = self.hub.delete_all()
        if status != "ok":
            raise Refused(self._answer_for(status), status)
        return 200, {"ok": True}

    # -- requests and models ---------------------------------------------------------

    def _chat(self, call):
        body = call.body
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise Refused(400, "bad_text")
        ids = body.get("attachments", [])
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            raise Refused(400, "bad_attachments")
        speak = body.get("speak") is True and self.voice is not None
        tracker = self.voice.track(call.context) if speak else None
        try:
            result = self.hub.submit(text, ids, origin=self.origin,
                                     on_finish=tracker.finished if tracker is not None else None)
        except UploadRefused as refused:
            raise Refused(refused.status, refused.code)
        if tracker is not None:
            tracker.accepted(result.query_id)
        if result.status != "accepted":
            raise Refused({"busy": 409}.get(result.status, 503), result.status)
        return 202, {"query_id": result.query_id, "status": result.status}

    def _stop(self, _call):
        self.hub.cancel()
        return 200, {"ok": True}

    def _model(self, call):
        body = call.body
        kind, value, effort = body.get("kind"), body.get("value"), body.get("effort")
        if (kind not in ("mode", "local", "cloud") or not isinstance(value, str) or not value.strip()
                or not (effort is None or isinstance(effort, str))):
            raise Refused(400, "bad_model")
        if kind == "mode":
            result = self.hub.switch_mode(value)
        elif kind == "local":
            result = self.hub.set_local_model(value)
        else:
            result = self.hub.set_cloud_model(value, effort)
        if not result.ok:
            raise Refused(409, result.reason or "refused")
        return 200, {"ok": True}

    # -- attachments -----------------------------------------------------------------

    def _upload(self, call):
        try:
            return 201, self.hub.add_upload(call.body["bytes"])
        except UploadRefused as refused:
            raise Refused(refused.status, refused.code)

    def _screenshot(self, _call):
        try:
            return 201, self.hub.capture_screenshot()
        except UploadRefused as refused:
            raise Refused(refused.status, refused.code)

    def _discard_upload(self, upload_id, _call):
        self.hub.discard_upload(upload_id)
        return 200, {"ok": True}

    def _attachment_support(self, _call):
        return 200, self.hub.attachment_support()

    # -- the PC's microphone (web surface only) ----------------------------------------

    @staticmethod
    def _session(body: dict) -> str:
        value = body.get("session")
        if not isinstance(value, str) or not _SESSION.match(value):
            raise Refused(400, "bad_session")
        return value

    def _mic_status(self, _call):
        return 200, self.hub.mic.status()

    def _mic_start(self, _call):
        session, reason = self.hub.mic.start()
        if session is None:
            raise Refused(409 if reason == "busy" else 503, reason or "unavailable")
        return 200, {"session": session, "max_seconds": MAX_RECORDING_SEC}

    def _mic_heartbeat(self, call):
        state = self.hub.mic.beat(self._session(call.body))
        if state is None:
            raise Refused(404, "unknown")
        return 200, {"state": state}

    def _mic_stop(self, call):
        text = self.hub.mic.stop(self._session(call.body))  # the transcript goes to this response only
        if text is None:
            raise Refused(404, "unknown")
        return 200, {"text": text}

    def _mic_cancel(self, call):
        self.hub.mic.cancel(self._session(call.body))
        return 200, {"ok": True}
