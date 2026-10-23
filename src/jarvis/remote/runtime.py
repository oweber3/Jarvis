"""Starting and stopping phone access with the daemon. Nothing runs unless the user turned it on."""
from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Optional

from ..debug import debug_log
from ..webchat import runtime as web_chat_runtime
from .networks import local_addresses
from .pairing import DeviceStore
from .server import RemoteServer, ServerSettings
from .tls import TLSError, tls_config

_lock = threading.RLock()
_server: Optional[RemoteServer] = None


def devices_directory(cfg) -> Path:
    """Paired devices live next to the database."""
    return Path(str(cfg.db_path)).expanduser().parent


def phone_links(cfg, port: Optional[int] = None):
    """Where the phone opens the app. With a certificate: ``https://<certificate name>:<port>/`` only, since
    the port then serves nothing but HTTPS. Without: ``http://<address>:<port>/`` for each private address of
    this PC, then its host name."""
    port = int(cfg.remote_access_port) if port is None else port
    if tls_config(cfg) is not None:
        name = str(getattr(cfg, "remote_access_tls_host", "") or "").strip() or _own_name()
        return [f"https://{name}:{port}/"] if name else []
    hosts = local_addresses()
    own = _own_name()
    if own:
        hosts.append(own)
    return [f"http://{host}:{port}/" for host in hosts]


def _own_name() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def start(cfg, *, backend=None, chat_backend=None) -> Optional[RemoteServer]:
    """Serve the phone app when ``remote_access_enabled`` is on. Returns the server, or ``None``.

    ``chat_backend`` is the web chat's backend for the shared hub, created only when the web chat has not
    already created it."""
    global _server
    if getattr(cfg, "remote_access_enabled", False) is not True:
        debug_log("remote: phone access is off", "remote")
        return None
    if backend is None:
        from .backend import DaemonBackend
        backend = DaemonBackend()
    tls = tls_config(cfg)
    settings = ServerSettings(
        host=cfg.remote_access_host,
        port=int(cfg.remote_access_port),
        quick_actions=list(cfg.remote_access_quick_actions),
        allow_confirm=bool(cfg.remote_access_allow_confirm),
        tls=tls,
        tls_host=str(getattr(cfg, "remote_access_tls_host", "") or ""),
    )
    with _lock:
        _stop_locked()
        hub = web_chat_runtime.acquire_hub(cfg, backend=chat_backend)
        server = RemoteServer(hub, DeviceStore(devices_directory(cfg)), backend, settings)
        try:
            server.start()
        except TLSError as exc:
            web_chat_runtime.release_hub()
            debug_log("remote: the HTTPS certificate could not be used", "remote")
            print(f"⚠️ Phone access could not start: {exc}", flush=True)
            print("   💡 Run python -m jarvis.remote cert to get a certificate, or clear the HTTPS settings "
                  "in Settings > Extras > Phone access.", flush=True)
            return None
        except OSError as exc:
            web_chat_runtime.release_hub()
            debug_log(f"remote: could not bind {settings.host}:{settings.port}: {type(exc).__name__}", "remote")
            print(f"⚠️ Phone access could not start on {settings.host}:{settings.port}", flush=True)
            print(f"   💡 {exc.strerror or exc}. Pick another port in Settings > Extras > Phone access.", flush=True)
            return None
        _server = server
    print(f"📱 Phone access is on ({'HTTPS' if tls else 'HTTP'}, port {server.port})", flush=True)
    for link in phone_links(cfg, server.port):
        print(f"   🔗 {link}", flush=True)
    if tls is None:
        print("   🎙️ Talking to Jarvis from the phone needs HTTPS: python -m jarvis.remote cert", flush=True)
    print("   🔑 Pair a phone from the tray: Phone Access", flush=True)
    return server


def _stop_locked() -> None:
    global _server
    if _server is not None:
        try:
            _server.stop()
        except Exception as exc:
            debug_log(f"remote: stop failed: {type(exc).__name__}", "remote")
        _server = None
        web_chat_runtime.release_hub()


def stop() -> None:
    with _lock:
        _stop_locked()


def current() -> Optional[RemoteServer]:
    with _lock:
        return _server
