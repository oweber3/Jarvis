"""How the built page's files travel: built assets kept by the browser, text compressed when it is accepted.

The web chat's server and phone access's server both serve a build of the React app (``webchat/static`` and
``remote/static``). The files under ``/assets/`` are named by their content, so a new build has new names and the
browser may keep them for good; every other file (the page itself, the phone's manifest, icons and worklet) is
asked for again each time. Text files go gzip-compressed to a browser that accepts it. Only static files: API
answers are never compressed or kept. See ``webchat.spec.md``, Server guards.
"""
from __future__ import annotations

import gzip
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

ASSETS_CACHE_CONTROL = "public, max-age=31536000, immutable"
PAGE_CACHE_CONTROL = "no-cache"
COMPRESSIBLE = frozenset({".html", ".js", ".mjs", ".css", ".svg", ".json", ".webmanifest", ".map"})
# Smaller files gain nothing from compression.
MIN_COMPRESS_BYTES = 1024


def cache_control(path: str) -> str:
    """The ``Cache-Control`` of a static file by the path it was asked for."""
    return ASSETS_CACHE_CONTROL if path.startswith("/assets/") else PAGE_CACHE_CONTROL


def accepts_gzip(accept_encoding: Optional[str]) -> bool:
    """Whether an ``Accept-Encoding`` header takes gzip (named, or ``*``, and not refused with ``q=0``)."""
    for part in (accept_encoding or "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() not in ("gzip", "*"):
            continue
        quality = params.strip().lower()
        if quality.startswith("q="):
            try:
                if float(quality[2:]) <= 0:
                    continue
            except ValueError:
                continue
        return True
    return False


class StaticFiles:
    """The bytes of a static folder's files, read once and kept with their compressed form until a file changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: Dict[str, Tuple[Tuple[int, int], bytes, Optional[bytes]]] = {}

    def body(self, target: Path, accept_encoding: Optional[str]) -> Tuple[bytes, Optional[str]]:
        """The bytes to send for ``target`` and their ``Content-Encoding`` (``gzip``, or None for as they are)."""
        stat = target.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        key = str(target)
        with self._lock:
            known = self._files.get(key)
        if known is None or known[0] != version:
            raw = target.read_bytes()
            packed = None
            if target.suffix.lower() in COMPRESSIBLE and len(raw) >= MIN_COMPRESS_BYTES:
                packed = gzip.compress(raw, compresslevel=6, mtime=0)
                if len(packed) >= len(raw):
                    packed = None
            known = (version, raw, packed)
            with self._lock:
                self._files[key] = known
        _version, raw, packed = known
        if packed is not None and accepts_gzip(accept_encoding):
            return packed, "gzip"
        return raw, None

    @staticmethod
    def varies(target: Path) -> bool:
        """Whether the bytes sent for ``target`` depend on ``Accept-Encoding``."""
        return target.suffix.lower() in COMPRESSIBLE
