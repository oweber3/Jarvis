"""What a tool can know about the request and the call it runs in, held in context variables.

- The request scope is set by ``run_reply_engine`` for each request. ``run_tool_with_retries`` notes in
  it when a tool that returns outside content has run, so a later call in the same request can tell
  (routine changes are refused after it, ``routines.spec.md``, Prompt-injection boundary). An approved
  confirmation runs later on its own and is in no request.
- The call context is set by ``run_tool_with_retries`` for the duration of one call: the request's
  language, the allowed-tool snapshot of a background bridge request, the bridge request reference
  and, once the user has approved the call, the approved request.

Context variables follow the thread that runs the request, so concurrent requests never see each other.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterator, Optional


@dataclass
class RequestScope:
    outside_content: bool = False


@dataclass(frozen=True)
class CallContext:
    language: Optional[str] = None
    # A text request: its output stays out of the console and a confirmation's outcome returns to the chat.
    quiet: bool = False
    # Set when a background bridge runs the call: the tools that request may use.
    allowed_tools: Optional[FrozenSet[str]] = None
    request_ref: Optional[str] = None
    # The ``ConfirmationRequest`` the user approved for this exact call, when there was one.
    approval: Any = None


_request: ContextVar[Optional[RequestScope]] = ContextVar("jarvis_request_scope", default=None)
_call: ContextVar[Optional[CallContext]] = ContextVar("jarvis_tool_call", default=None)


@contextmanager
def request_scope() -> Iterator[RequestScope]:
    """A fresh scope for one request; nothing seen in an earlier request carries into it."""
    scope = RequestScope()
    token = _request.set(scope)
    try:
        yield scope
    finally:
        _request.reset(token)


def note_outside_content() -> None:
    """Record that a tool returning outside content (web, files, screen, clipboard, window titles, MCP) ran in
    this request."""
    scope = _request.get()
    if scope is not None:
        scope.outside_content = True


def outside_content_seen() -> bool:
    scope = _request.get()
    return scope is not None and scope.outside_content


@contextmanager
def tool_call(context: CallContext) -> Iterator[CallContext]:
    token = _call.set(context)
    try:
        yield context
    finally:
        _call.reset(token)


def current_call() -> CallContext:
    """The context of the tool call in progress on this thread (empty outside one)."""
    return _call.get() or CallContext()
