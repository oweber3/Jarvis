"""Process-wide handle to the running bridge service of the active cloud reply mode.

Set by the reply-mode controller, read by the reply engine, the chat Stop button and the voice
listener's stop handling. At most one bridge service is registered at a time.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

_lock = threading.Lock()
_service: Optional[Any] = None


def set_service(service: Optional[Any]) -> None:
    global _service
    with _lock:
        _service = service


def get_service() -> Optional[Any]:
    with _lock:
        return _service


def request_active() -> bool:
    """True while a background bridge request is in flight."""
    service = get_service()
    return service is not None and bool(service.is_busy())


def cancel_active_request(reason: str) -> bool:
    """Cancel the in-flight bridge request, if any. Safe to call with no bridge."""
    service = get_service()
    return service is not None and bool(service.cancel_active(reason))
