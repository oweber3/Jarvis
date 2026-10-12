"""Whether gesture snapping is running, shared with other processes for the desktop app's pointer ring.

The daemon may run in another process from the desktop app, so the flag lives in a small memory-mapped file in
the temp folder, like the voice levels (``jarvis/voice_levels.py``). It holds one flag and the wall-clock time it
was written, nothing else. A flag without a recent heartbeat reads as off, so a daemon that dies cannot leave the
ring on. See ``gestures.spec.md``, Shared on flag for the desktop app.
"""
from __future__ import annotations

import mmap
import os
import struct
import tempfile
import threading
import time
from typing import Optional

from ..debug import debug_log

MAX_AGE_S = 1.0
_SEQUENCE = struct.Struct("<Q")
_DATA = struct.Struct("<Bd")  # on, wall-clock time written
_SIZE = _SEQUENCE.size + _DATA.size
_READ_ATTEMPTS = 50
_RETRY_S = 5.0


class SnapStateChannel:
    """The flag in a memory-mapped file at ``path``; every process that opens the same path shares it.

    A sequence number (odd while a write is in progress) keeps a reader in another process from taking half of
    one write and half of another."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._map: Optional[mmap.mmap] = None
        self._failed_at: Optional[float] = None

    def _open(self) -> Optional[mmap.mmap]:
        if self._map is not None:
            return self._map
        now = time.monotonic()
        if self._failed_at is not None and now - self._failed_at < _RETRY_S:
            return None
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                if os.fstat(fd).st_size < _SIZE:
                    os.ftruncate(fd, _SIZE)
                self._map = mmap.mmap(fd, _SIZE)
            finally:
                os.close(fd)
        except (OSError, ValueError) as exc:
            if self._failed_at is None:
                debug_log(f"gesture snapping state unavailable: {type(exc).__name__}", "gestures")
            self._failed_at = now
            return None
        return self._map

    def publish(self, on: bool, now: Optional[float] = None) -> None:
        """Record whether gesture snapping is running. Never raises: the ring is decoration, not a dependency."""
        stamp = time.time() if now is None else now
        with self._lock:
            view = self._open()
            if view is None:
                return
            try:
                sequence = _SEQUENCE.unpack_from(view, 0)[0]
                if sequence % 2:
                    sequence += 1
                _SEQUENCE.pack_into(view, 0, sequence + 1)
                _DATA.pack_into(view, _SEQUENCE.size, 1 if on else 0, stamp)
                _SEQUENCE.pack_into(view, 0, sequence + 2)
            except (ValueError, struct.error):
                pass

    def is_on(self, now: Optional[float] = None) -> bool:
        """True when the flag is on and was written within ``MAX_AGE_S``."""
        view = self._open()
        if view is None:
            return False
        for _ in range(_READ_ATTEMPTS):
            try:
                before = _SEQUENCE.unpack_from(view, 0)[0]
                on, stamp = _DATA.unpack_from(view, _SEQUENCE.size)
                after = _SEQUENCE.unpack_from(view, 0)[0]
            except (ValueError, struct.error):
                return False
            if before == after and before and before % 2 == 0:
                break
        else:
            return False
        now = time.time() if now is None else now
        return bool(on) and 0.0 <= now - stamp <= MAX_AGE_S


_channel = SnapStateChannel(os.path.join(tempfile.gettempdir(), "jarvis_gesture_snap"))


def publish(on: bool, now: Optional[float] = None) -> None:
    """Record on the shared channel whether gesture snapping is running."""
    _channel.publish(on, now)


def is_on(now: Optional[float] = None) -> bool:
    """Whether gesture snapping is running, as the daemon last said within ``MAX_AGE_S``."""
    return _channel.is_on(now)
