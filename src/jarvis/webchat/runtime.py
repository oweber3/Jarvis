"""Starting and stopping the web chat with the daemon. Nothing runs unless the user turned it on."""
from __future__ import annotations

import threading
from typing import Optional

from ..debug import debug_log
from ..memory.chat_store import ChatStore
from .hub import ChatHub
from .server import ServerSettings, WebChatServer

LOOPBACK = "127.0.0.1"

_lock = threading.Lock()
_server: Optional[WebChatServer] = None
_store: Optional[ChatStore] = None


def start(cfg, *, backend=None) -> Optional[WebChatServer]:
    """Serve the web chat when ``web_chat_enabled`` is on. Returns the server, or ``None``."""
    global _server, _store
    if getattr(cfg, "web_chat_enabled", False) is not True:
        debug_log("webchat: the web chat is off", "webchat")
        return None
    if backend is None:
        from .backend import DaemonBackend
        backend = DaemonBackend()
    port = int(cfg.web_chat_port)
    with _lock:
        _stop_locked()
        store = ChatStore(str(cfg.db_path))
        server = WebChatServer(ChatHub(backend, store), ServerSettings(host=LOOPBACK, port=port))
        try:
            server.start()
        except OSError as exc:
            server.hub.stop()
            store.close()
            debug_log(f"webchat: could not bind {LOOPBACK}:{port}: {type(exc).__name__}", "webchat")
            print(f"⚠️ The web chat could not start on port {port}", flush=True)
            print(f"   💡 {exc.strerror or exc}. Pick another port in Settings > Web Chat.", flush=True)
            return None
        _server, _store = server, store
    print(f"💬 Web chat is on (port {server.port}, this PC only)", flush=True)
    return server


def _stop_locked() -> None:
    global _server, _store
    if _server is not None:
        try:
            _server.stop()
        except Exception as exc:
            debug_log(f"webchat: stop failed: {type(exc).__name__}", "webchat")
        _server = None
    if _store is not None:
        _store.close()
        _store = None


def stop() -> None:
    with _lock:
        _stop_locked()


def current() -> Optional[WebChatServer]:
    with _lock:
        return _server
