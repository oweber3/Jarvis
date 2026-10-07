"""The Stop signal of one request, held in a context variable.

``daemon`` sets it around a typed request (``cancel_scope``); the reply engine checks it between steps
(``check_cancelled``) and hands it to the model call (``current_cancel``), so Stop ends the work instead of
only hiding its result. A request with no scope, a voice request for example, has nothing to cancel.

Context variables follow the thread that runs the request, so concurrent requests never see each other's
signal. See ``reply.spec.md``, Stopping a reply.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from ..llm.errors import RequestCancelled

_cancel: ContextVar[Optional[threading.Event]] = ContextVar("jarvis_request_cancel", default=None)


@contextmanager
def cancel_scope(event: threading.Event) -> Iterator[threading.Event]:
    """Make ``event`` the Stop signal of the request that runs inside this block."""
    token = _cancel.set(event)
    try:
        yield event
    finally:
        _cancel.reset(token)


def current_cancel() -> Optional[threading.Event]:
    return _cancel.get()


def check_cancelled() -> None:
    """Raise :class:`RequestCancelled` when Stop has been pressed for the running request."""
    event = _cancel.get()
    if event is not None and event.is_set():
        raise RequestCancelled()
