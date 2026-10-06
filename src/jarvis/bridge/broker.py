"""Request broker for the background reply bridges.

Pure lifecycle logic with no I/O. Every transition is a compare-and-set under one lock, so the
first terminal transition wins and later ones change nothing. Each request is owned by the session
and turn Jarvis created for it (a Codex thread and turn, or a Claude process and its turn); calls
from any other session are refused. See ``bridge.spec.md``.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..debug import debug_log


class State(str, Enum):
    QUEUED = "queued"
    SUBMITTED = "submitted"
    ACTIVE = "active"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL = frozenset({State.COMPLETED, State.FAILED, State.CANCELLED, State.EXPIRED})
_COMPLETION_STATUSES = ("completed", "needs_user_input", "failed")
_TRUNCATION_MARKER = " [truncated]"
# Finished requests kept for late, stale calls (which are refused with their finished reason). Older
# ones are forgotten so utterances, context and results do not accumulate for the service's life.
_FINISHED_KEPT = 16


class BusyError(Exception):
    """Active plus queued requests already fill the configured capacity."""


class PayloadTooLargeError(Exception):
    """A request field exceeds its bound."""


@dataclass(frozen=True)
class BrokerLimits:
    deadline_sec: float = 90.0
    queue_limit: int = 1
    max_tool_calls: int = 8
    max_utterance_chars: int = 4000
    max_context_chars: int = 4000
    max_arguments_bytes: int = 8192
    max_result_chars: int = 4000
    max_reply_chars: int = 2000
    max_referents: int = 5
    max_other_referents: int = 4


@dataclass
class ExecDecision:
    # run | stored | uncertain | refused
    kind: str
    reason: Optional[str] = None
    text: Optional[str] = None
    status: Optional[str] = None


@dataclass
class TurnOutcome:
    deliver: bool = False
    reply: Optional[str] = None
    is_question: bool = False
    reason: Optional[str] = None


@dataclass
class _Call:
    tool: str
    key: str
    # inflight | ok | error | uncertain | awaiting
    status: str = "inflight"
    text: str = ""


@dataclass
class Request:
    id: str
    utterance: str
    context: List[Dict[str, Any]]
    origin: str
    language: Optional[str]
    allowed_tools: Dict[str, Any]
    created_at: float
    expires_at: float
    state: State = State.QUEUED
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    failure_reason: Optional[str] = None
    calls: Dict[str, _Call] = field(default_factory=dict)
    by_key: Dict[str, str] = field(default_factory=dict)
    confirmation_id: Optional[str] = None
    # The request's desktop context (``foreground_window``, ``desktop_referents``, ``other_referents``,
    # ``desktop_referents_note``), each present only when shared and non-empty.
    desktop: Dict[str, Any] = field(default_factory=dict)


def _bounded_desktop(desktop: Optional[Mapping[str, Any]], lim: "BrokerLimits") -> Dict[str, Any]:
    """A private copy of the request's desktop context: known keys only, lists bounded."""
    source = dict(desktop or {})
    bounded: Dict[str, Any] = {}
    if isinstance(source.get("foreground_window"), Mapping):
        bounded["foreground_window"] = dict(source["foreground_window"])
    for key, limit in (("desktop_referents", lim.max_referents), ("other_referents", lim.max_other_referents)):
        records = [dict(r) for r in list(source.get(key) or [])[:limit]]
        if records:
            bounded[key] = records
    if bounded and source.get("desktop_referents_note"):
        bounded["desktop_referents_note"] = str(source["desktop_referents_note"])
    return bounded


def _bound(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _TRUNCATION_MARKER


def _canonical(tool: str, args: Dict[str, Any]) -> str:
    return tool + "\x00" + json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


class Broker:
    def __init__(
        self,
        limits: Optional[BrokerLimits] = None,
        clock: Callable[[], float] = time.monotonic,
        validate_args: Optional[Callable[[Any, Dict[str, Any]], Optional[str]]] = None,
        log_tag: str = "bridge",
    ):
        self._limits = limits or BrokerLimits()
        self._log_tag = log_tag
        self._clock = clock
        self._validate = validate_args or (lambda schema, args: None)
        self._lock = threading.RLock()
        self._requests: Dict[str, Request] = {}
        self._confirmations: Dict[str, str] = {}

    # -- internals (callers hold the lock) ------------------------------------

    def _log(self, req: Request, event: str) -> None:
        debug_log(f"request {req.id[:8]} {event}", self._log_tag)

    def _to_terminal(self, req: Request, state: State, reason: Optional[str] = None) -> bool:
        if req.state in TERMINAL:
            return False
        req.state = state
        for call in req.calls.values():
            if call.status == "inflight":
                call.status = "uncertain"
        if state is State.FAILED:
            req.failure_reason = reason
        if req.confirmation_id:
            self._confirmations.pop(req.confirmation_id, None)
        self._log(req, f"-> {state.value}" + (f" ({reason})" if reason else ""))
        return True

    def _refresh(self, req: Request) -> None:
        if req.state not in TERMINAL and self._clock() > req.expires_at:
            self._to_terminal(req, State.EXPIRED, "deadline")

    def _forget_finished(self) -> None:
        finished = [rid for rid, r in self._requests.items() if r.state in TERMINAL]
        for rid in finished[:max(0, len(finished) - _FINISHED_KEPT)]:
            del self._requests[rid]

    def _find(self, request_id: str) -> Optional[Request]:
        req = self._requests.get(request_id)
        if req is not None:
            self._refresh(req)
        return req

    @staticmethod
    def _owner_reason(req: Request, thread_id: Optional[str], turn_id: Optional[str]) -> Optional[str]:
        if req.thread_id is None or thread_id != req.thread_id:
            return "wrong_thread"
        if req.turn_id is None or turn_id != req.turn_id:
            return "wrong_turn"
        return None

    @staticmethod
    def _finished_reason(req: Request) -> Optional[str]:
        if req.state is State.CANCELLED:
            return "cancelled"
        if req.state is State.EXPIRED:
            return "expired"
        if req.state in TERMINAL:
            return "request_finished"
        return None

    # -- creation and session assignment ---------------------------------------------

    def submit_request(
        self,
        utterance: str,
        context: List[Dict[str, Any]],
        origin: str,
        language: Optional[str],
        allowed_tools: Dict[str, Any],
        desktop: Optional[Mapping[str, Any]] = None,
    ) -> Request:
        lim = self._limits
        if len(utterance) > lim.max_utterance_chars:
            raise PayloadTooLargeError("utterance")
        bounded_context = [
            {**m, "content": _bound(str(m.get("content", "")), lim.max_context_chars)}
            for m in (context or [])
        ]
        with self._lock:
            live = 0
            for r in self._requests.values():
                self._refresh(r)
                if r.state not in TERMINAL:
                    live += 1
            if live >= 1 + lim.queue_limit:
                raise BusyError("bridge is busy")
            self._forget_finished()
            now = self._clock()
            req = Request(
                id=secrets.token_urlsafe(24),
                utterance=utterance,
                context=bounded_context,
                origin=origin,
                language=language,
                allowed_tools=dict(allowed_tools),
                created_at=now,
                expires_at=now + lim.deadline_sec,
                desktop=_bounded_desktop(desktop, lim),
            )
            self._requests[req.id] = req
            self._log(req, "queued")
            return req

    def assign_thread(self, request_id: str, thread_id: str) -> bool:
        """Record the session Jarvis created for the request (``queued`` -> ``submitted``)."""
        with self._lock:
            req = self._find(request_id)
            if req is None or req.state is not State.QUEUED or not thread_id:
                return False
            req.thread_id = thread_id
            req.state = State.SUBMITTED
            req.expires_at = self._clock() + self._limits.deadline_sec
            self._log(req, "-> submitted")
            return True

    def assign_turn(self, request_id: str, turn_id: str) -> bool:
        """Record the turn carrying the request (``submitted`` -> ``active``)."""
        with self._lock:
            req = self._find(request_id)
            if req is None or req.state is not State.SUBMITTED or req.turn_id is not None or not turn_id:
                return False
            req.turn_id = turn_id
            req.state = State.ACTIVE
            self._log(req, "-> active")
            return True

    # -- queries ----------------------------------------------------------------

    def state_of(self, request_id: str) -> Optional[State]:
        with self._lock:
            req = self._find(request_id)
            return req.state if req else None

    def failure_reason(self, request_id: str) -> Optional[str]:
        with self._lock:
            req = self._requests.get(request_id)
            return req.failure_reason if req else None

    def request_of(self, request_id: str) -> Optional[Request]:
        """The request, for reading the fields fixed at creation (utterance, context, tools)."""
        with self._lock:
            return self._find(request_id)

    def remaining_sec(self, request_id: str) -> float:
        with self._lock:
            req = self._find(request_id)
            return max(0.0, req.expires_at - self._clock()) if req else 0.0

    def owner_of(self, request_id: str) -> Tuple[Optional[str], Optional[str]]:
        with self._lock:
            req = self._requests.get(request_id)
            return (req.thread_id, req.turn_id) if req else (None, None)

    def outcome_of(self, request_id: str, call_id: str) -> Optional[str]:
        with self._lock:
            req = self._requests.get(request_id)
            call = req.calls.get(call_id) if req else None
            return call.status if call else None

    # -- model-facing operations ---------------------------------------------------

    def allowed_tool_names(self, request_id: str) -> frozenset:
        """The names in the request's allowed-tool snapshot (empty for an unknown request)."""
        with self._lock:
            req = self._requests.get(request_id)
            return frozenset(req.allowed_tools) if req is not None else frozenset()

    def begin_execute(self, request_id: str, call_id: str, tool_name: str, arguments: Any,
                      thread_id: Optional[str], turn_id: Optional[str]) -> ExecDecision:
        lim = self._limits
        with self._lock:
            req = self._find(request_id)
            if req is None:
                return ExecDecision("refused", "unknown_request")

            def refuse(reason: str, detail: Optional[str] = None) -> ExecDecision:
                # The detail goes back to the model only; it names argument keys, so it is never logged.
                self._log(req, f"execute refused ({reason})")
                return ExecDecision("refused", reason,
                                    text=_bound(detail, self._limits.max_result_chars) if detail else None)

            finished = self._finished_reason(req)
            if finished:
                return refuse(finished)
            bad_owner = self._owner_reason(req, thread_id, turn_id)
            if bad_owner:
                return refuse(bad_owner)
            if req.state is State.AWAITING_CONFIRMATION:
                return refuse("awaiting_confirmation")
            if req.state is not State.ACTIVE:
                return refuse("not_active")
            if tool_name not in req.allowed_tools:
                return refuse("tool_not_allowed")
            if not isinstance(arguments, dict):
                return refuse("invalid_arguments", "arguments is not an object")
            if len(json.dumps(arguments, default=str).encode("utf-8")) > lim.max_arguments_bytes:
                return refuse("payload_too_large")
            problem = self._validate(req.allowed_tools[tool_name], arguments)
            if problem is not None:
                return refuse("invalid_arguments", problem)

            key = _canonical(tool_name, arguments)
            existing_id = call_id if call_id in req.calls else req.by_key.get(key)
            if existing_id is not None:
                prior = req.calls[existing_id]
                if prior.key != key:
                    return refuse("call_id_reuse")
                if prior.status == "inflight":
                    return refuse("in_flight")
                if prior.status == "awaiting":
                    return refuse("awaiting_confirmation")
                if prior.status == "uncertain":
                    return ExecDecision("uncertain", status="uncertain",
                                        text="The outcome of this action is uncertain; it was not repeated.")
                return ExecDecision("stored", status=prior.status, text=prior.text)
            if len(req.calls) >= lim.max_tool_calls:
                return refuse("too_many_calls")

            req.calls[call_id] = _Call(tool=tool_name, key=key)
            req.by_key[key] = call_id
            return ExecDecision("run")

    def record_result(self, request_id: str, call_id: str, status: str, text: str) -> bool:
        with self._lock:
            req = self._requests.get(request_id)
            call = req.calls.get(call_id) if req else None
            if call is None or call.status != "inflight":
                return False
            call.status = "ok" if status == "ok" else "error"
            call.text = _bound(text or "", self._limits.max_result_chars)
            return True

    def result_text(self, request_id: str, call_id: str) -> str:
        """The bounded text stored for a recorded call (empty when none)."""
        with self._lock:
            req = self._requests.get(request_id)
            call = req.calls.get(call_id) if req else None
            return call.text if call else ""

    def record_uncertain(self, request_id: str, call_id: str) -> None:
        with self._lock:
            req = self._requests.get(request_id)
            call = req.calls.get(call_id) if req else None
            if call is not None:
                call.status = "uncertain"

    def mark_awaiting_confirmation(self, request_id: str, call_id: str, confirmation_id: str) -> bool:
        with self._lock:
            req = self._find(request_id)
            if req is None or req.state is not State.ACTIVE:
                return False
            req.state = State.AWAITING_CONFIRMATION
            req.confirmation_id = confirmation_id
            self._confirmations[confirmation_id] = req.id
            if call_id in req.calls:
                req.calls[call_id].status = "awaiting"
            self._log(req, "-> awaiting_confirmation")
            return True

    def confirmation_resolved(self, confirmation_id: str, outcome: str) -> None:
        with self._lock:
            rid = self._confirmations.get(confirmation_id)
            req = self._requests.get(rid) if rid else None
            if req is None or req.state is not State.AWAITING_CONFIRMATION:
                return
            if outcome == "approved":
                self._to_terminal(req, State.COMPLETED)
            else:
                self._to_terminal(req, State.CANCELLED, outcome)

    def finish_turn(self, request_id: str, turn_id: str, status: str, failure: Optional[str] = None,
                    answer: Optional[Tuple[Any, Any]] = None) -> TurnOutcome:
        """Apply the owning turn's terminal status and its final answer ``(status, reply)``.

        Delivery needs a ``completed`` turn whose final answer has a known status and a non-empty
        reply. The first terminal transition wins, so a request is delivered at most once.
        """
        with self._lock:
            req = self._find(request_id)
            if req is None or req.turn_id is None or turn_id != req.turn_id:
                return TurnOutcome()
            if req.state is not State.ACTIVE:
                return TurnOutcome()
            if status != "completed":
                reason = failure or ("interrupted" if status == "interrupted" else "turn_failed")
                self._to_terminal(req, State.FAILED, reason)
                return TurnOutcome(reason=reason)
            answer_status, reply = answer if answer is not None else (None, None)
            text = reply.strip() if isinstance(reply, str) else ""
            if answer_status not in _COMPLETION_STATUSES or not text:
                self._to_terminal(req, State.FAILED, "no_answer")
                return TurnOutcome(reason="no_answer")
            text = _bound(text, self._limits.max_reply_chars)
            if answer_status == "failed":
                self._to_terminal(req, State.FAILED, "reported_failure")
                return TurnOutcome(True, text, reason="reported_failure")
            self._to_terminal(req, State.COMPLETED)
            return TurnOutcome(True, text, is_question=answer_status == "needs_user_input")

    # -- control ----------------------------------------------------------------

    def fail(self, request_id: str, reason: str) -> bool:
        with self._lock:
            req = self._find(request_id)
            return req is not None and self._to_terminal(req, State.FAILED, reason)

    def cancel(self, request_id: str, reason: str) -> bool:
        with self._lock:
            req = self._find(request_id)
            return req is not None and self._to_terminal(req, State.CANCELLED, reason)

    def cancel_all(self, reason: str) -> int:
        with self._lock:
            return sum(1 for r in list(self._requests.values())
                       if self._to_terminal(r, State.CANCELLED, reason))

    def expire_due(self) -> int:
        with self._lock:
            before = sum(1 for r in self._requests.values() if r.state in TERMINAL)
            for r in list(self._requests.values()):
                self._refresh(r)
            return sum(1 for r in self._requests.values() if r.state in TERMINAL) - before
