"""Debug logging utilities for Jarvis."""
import sys
import threading
import time
from typing import Optional
from .config import load_settings


_last_check_time: float = 0.0
_cached_voice_debug: Optional[bool] = None
_CACHE_TTL_SECONDS: float = 2.0


# Loading settings can itself log (a failed credential read), which would re-enter the check.
_checking = threading.local()


def _is_debug_enabled() -> bool:
    global _last_check_time, _cached_voice_debug
    now = time.time()
    if _cached_voice_debug is None or (now - _last_check_time) > _CACHE_TTL_SECONDS:
        if getattr(_checking, "active", False):
            return bool(_cached_voice_debug)
        _checking.active = True
        try:
            _cached_voice_debug = bool(load_settings().voice_debug)
        except Exception:
            _cached_voice_debug = False
        finally:
            _checking.active = False
        _last_check_time = now
    return bool(_cached_voice_debug)


def debug_log(message: str, category: str = "debug") -> None:
    """Unified debug logging function for Jarvis.

    Args:
        message: The debug message to log
        category: The log category (e.g., "debug", "voice", "echo", "tts", etc.)
    """
    if not _is_debug_enabled():
        return
    try:
        print(f"[{category:^10}] {message}", file=sys.stderr)
    except Exception:
        pass
