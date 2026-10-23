"""The phone access server's certificate: checked when the server starts, reloaded when the files change.

Standard library ``ssl`` only. A bad pair stops the start with a message that names what is wrong and never
quotes the key. See ``remote.spec.md``, HTTPS.
"""
from __future__ import annotations

import os
import ssl
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from ..debug import debug_log


class TLSError(Exception):
    """The certificate or key cannot be used; the message says why in plain words."""


@dataclass(frozen=True)
class TLSConfig:
    cert: str
    key: str


def tls_config(cfg) -> Optional[TLSConfig]:
    """The certificate settings when both paths are set, otherwise ``None`` (plain HTTP)."""
    cert = str(getattr(cfg, "remote_access_tls_cert", "") or "").strip()
    key = str(getattr(cfg, "remote_access_tls_key", "") or "").strip()
    return TLSConfig(cert, key) if cert and key else None


def _stamp(path: str) -> Optional[Tuple[int, int]]:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


class ReloadingContext:
    """A server ``SSLContext`` for ``cert`` and ``key`` that follows the files.

    ``context()`` is cheap (two ``stat`` calls) and builds a new context when either file changed. A pair
    that no longer loads keeps the last good one in service."""

    def __init__(self, cert: str, key: str) -> None:
        self._cert, self._key = cert, key
        self._lock = threading.Lock()
        self._stamps = (None, None)
        self._context = self._load()
        self._stamps = (_stamp(cert), _stamp(key))

    def _load(self) -> ssl.SSLContext:
        if not os.path.isfile(self._cert):
            raise TLSError("The HTTPS certificate file was not found.")
        if not os.path.isfile(self._key):
            raise TLSError("The HTTPS key file was not found.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(self._cert, self._key)
        except (ssl.SSLError, OSError, ValueError):
            raise TLSError("The HTTPS certificate and key could not be loaded "
                           "(they must be a matching pair in PEM format).") from None
        return context

    def context(self) -> ssl.SSLContext:
        stamps = (_stamp(self._cert), _stamp(self._key))
        with self._lock:
            if stamps != self._stamps:
                self._stamps = stamps
                try:
                    self._context = self._load()
                    debug_log("remote: HTTPS certificate reloaded", "remote")
                except TLSError:
                    debug_log("remote: HTTPS certificate changed but could not be loaded, keeping the old one",
                              "remote")
            return self._context
