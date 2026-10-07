"""Live voice loudness for the orb: how loud the microphone and Jarvis's own voice are, as 0..1 levels.

The listener and dictation publish the microphone's level and the Piper voice publishes
the level of what it plays. The desktop orb may run in another process from the daemon
(dev mode runs the daemon as a subprocess), so the levels live in a small memory-mapped
file in the temp folder that both processes open. Only two numbers and the time each was
measured are shared: never audio, never anything else.

``current`` returns Jarvis's voice while it is playing, otherwise the microphone, so the
orb follows whoever is speaking without knowing the assistant's state.
"""
from __future__ import annotations

import math
import mmap
import os
import struct
import tempfile
import threading
import time
from enum import IntEnum
from typing import Optional

from .debug import debug_log

MAX_AGE_S = 0.3                 # older levels read as absent, so a stopped producer never freezes the orb
_FLOOR_DB, _CEILING_DB = -52.0, -6.0   # quiet-room noise reads as 0, loud speech near 1
_SEQUENCE = struct.Struct("<Q")
_DATA = struct.Struct("<dd")    # level, wall-clock time measured
_SLOT = struct.Struct("<Qdd")   # one voice's slot: sequence then data
_READ_ATTEMPTS = 50
_RETRY_S = 5.0


class Voice(IntEnum):
    MICROPHONE = 0
    JARVIS = 1


def level_from_rms(rms: float, full_scale: float = 1.0) -> float:
    """Loudness 0..1 of audio with root-mean-square ``rms`` on a decibel scale (``full_scale`` is 0 dBFS)."""
    if rms <= 0.0 or full_scale <= 0.0:
        return 0.0
    db = 20.0 * math.log10(rms / full_scale)
    return min(1.0, max(0.0, (db - _FLOOR_DB) / (_CEILING_DB - _FLOOR_DB)))


class LevelChannel:
    """The two levels in a memory-mapped file at ``path``; every process that opens the same path shares them.

    Each slot is guarded by a sequence number (odd while a write is in progress), so a
    reader in another process never takes half of one write and half of another.
    """

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
        size = _SLOT.size * len(Voice)
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                if os.fstat(fd).st_size < size:
                    os.ftruncate(fd, size)
                self._map = mmap.mmap(fd, size)
            finally:
                os.close(fd)
        except (OSError, ValueError) as exc:
            if self._failed_at is None:
                debug_log(f"voice levels unavailable: {type(exc).__name__}", "voice")
            self._failed_at = now
            return None
        return self._map

    def publish(self, voice: Voice, level: float, now: Optional[float] = None) -> None:
        """Record ``voice``'s level (0..1). Never raises: the orb is decoration, not a dependency."""
        level = min(1.0, max(0.0, float(level)))
        stamp = time.time() if now is None else now
        with self._lock:
            view = self._open()
            if view is None:
                return
            offset = _SLOT.size * voice
            try:
                sequence = _SEQUENCE.unpack_from(view, offset)[0]
                if sequence % 2:
                    sequence += 1
                # Odd while the data changes, even once it is whole: three separate writes, in order.
                _SEQUENCE.pack_into(view, offset, sequence + 1)
                _DATA.pack_into(view, offset + _SEQUENCE.size, level, stamp)
                _SEQUENCE.pack_into(view, offset, sequence + 2)
            except (ValueError, struct.error):
                pass

    def level(self, voice: Voice, now: Optional[float] = None) -> Optional[float]:
        """``voice``'s level if it was measured within ``MAX_AGE_S``, otherwise None."""
        view = self._open()
        if view is None:
            return None
        offset = _SLOT.size * voice
        for _ in range(_READ_ATTEMPTS):
            try:
                before = _SEQUENCE.unpack_from(view, offset)[0]
                level, stamp = _DATA.unpack_from(view, offset + _SEQUENCE.size)
                after = _SEQUENCE.unpack_from(view, offset)[0]
            except (ValueError, struct.error):
                return None
            # An odd or zero sequence is a write in progress (or none yet); a changed one, a write in between.
            if before == after and before and before % 2 == 0:
                break
        else:
            return None
        now = time.time() if now is None else now
        if not 0.0 <= now - stamp <= MAX_AGE_S:
            return None
        return min(1.0, max(0.0, level))

    def current(self, now: Optional[float] = None) -> Optional[float]:
        """The level the orb follows: Jarvis's voice while it plays, otherwise the microphone."""
        jarvis = self.level(Voice.JARVIS, now)
        return jarvis if jarvis is not None else self.level(Voice.MICROPHONE, now)


_channel = LevelChannel(os.path.join(tempfile.gettempdir(), "jarvis_voice_levels"))


def publish(voice: Voice, level: float, now: Optional[float] = None) -> None:
    """Record ``voice``'s level on the shared channel, measured at ``now`` (wall clock; default: now)."""
    _channel.publish(voice, level, now)


def current() -> Optional[float]:
    """The level the orb follows, from the shared channel."""
    return _channel.current()
