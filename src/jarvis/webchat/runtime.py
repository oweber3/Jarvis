"""Starting and stopping the web chat with the daemon. Nothing runs unless the user turned it on.

The chat hub and its store are shared with phone access (``remote/remote.spec.md``, One hub, two servers):
``acquire_hub`` hands out the one hub, creating it for the first user, and ``release_hub`` lets the store go
when the last user has released it. Each server starts and stops the hub it was given.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from ..debug import debug_log
from ..memory.chat_store import ChatStore
from .hub import ChatHub
from .server import ServerSettings, WebChatServer

LOOPBACK = "127.0.0.1"


@dataclass
class _Shared:
    hub: ChatHub
    store: ChatStore
    users: int = 0


_lock = threading.RLock()
_server: Optional[WebChatServer] = None
_shared: Optional[_Shared] = None


def acquire_hub(cfg, *, backend=None) -> ChatHub:
    """The chat hub shared by the web chat and phone access, created on first use. Every call is paired
    with one ``release_hub``. The hub is not started here: the server that serves it starts and stops it."""
    global _shared
    with _lock:
        if _shared is None:
            if backend is None:
                from .backend import DaemonBackend
                backend = DaemonBackend()
            store = ChatStore(str(cfg.db_path))
            _shared = _Shared(ChatHub(backend, store), store)
            debug_log("webchat: chat hub created", "webchat")
        _shared.users += 1
        return _shared.hub


def release_hub() -> None:
    """Give the hub back; the last release closes the chat store."""
    global _shared
    with _lock:
        if _shared is None:
            return
        _shared.users -= 1
        if _shared.users <= 0:
            _shared.store.close()
            _shared = None
            debug_log("webchat: chat hub released", "webchat")


def start(cfg, *, backend=None) -> Optional[WebChatServer]:
    """Serve the web chat when ``web_chat_enabled`` is on. Returns the server, or ``None``."""
    global _server
    if getattr(cfg, "web_chat_enabled", False) is not True:
        debug_log("webchat: the web chat is off", "webchat")
        return None
    port = int(cfg.web_chat_port)
    with _lock:
        _stop_locked()
        hub = acquire_hub(cfg, backend=backend)
        server = WebChatServer(hub, ServerSettings(host=LOOPBACK, port=port))
        try:
            server.start()
        except OSError as exc:
            release_hub()
            debug_log(f"webchat: could not bind {LOOPBACK}:{port}: {type(exc).__name__}", "webchat")
            print(f"⚠️ The web chat could not start on port {port}", flush=True)
            print(f"   💡 {exc.strerror or exc}. Pick another port in Settings > Extras > Web chat.", flush=True)
            return None
        _server = server
    print(f"💬 Web chat is on (port {server.port}, this PC only)", flush=True)
    return server


def _stop_locked() -> None:
    global _server
    if _server is not None:
        try:
            _server.stop()
        except Exception as exc:
            debug_log(f"webchat: stop failed: {type(exc).__name__}", "webchat")
        _server = None
        release_hub()


def stop() -> None:
    with _lock:
        _stop_locked()


def current() -> Optional[WebChatServer]:
    with _lock:
        return _server
