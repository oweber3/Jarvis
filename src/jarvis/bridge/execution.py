"""Runs one ``jarvis_execute`` call through the central tool path on the request's query thread.

Shared by every bridge so validation, dedupe, confirmation, redaction and bounds behave the same
whichever model asked. See ``bridge.spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ..debug import debug_log
from ..tools.schema_validation import validate_arguments
from ..tools.types import ToolImage
from ..utils.redact import redact
from .broker import Broker

AWAITING_TEXT = "Jarvis is asking the user to confirm. Make no more calls and end this turn."


@dataclass
class ToolCallRunner:
    broker: Broker
    store: Any
    executor: Callable[..., Any]
    cfg: Any
    log_tag: str = "bridge"

    def execute(self, rid: str, call_id: str, envelope: Any, thread_id: Any, turn_id: Any, db: Any,
                language: Optional[str], quiet: bool, redacted: str
                ) -> Tuple[Dict[str, Any], Optional[str], Tuple[ToolImage, ...]]:
        """Run the call ``envelope`` (``request_id``, ``tool_name``, ``arguments``).

        Returns the result data for the model, the question Jarvis asks when the call now waits for the
        user's confirmation, and the images a successful, freshly run tool hands the model.
        """
        if not isinstance(envelope, dict):
            return {"status": "refused", "reason": "invalid_arguments",
                    "error": validate_arguments(None, envelope) or "the call is not an object"}, None, ()
        if envelope.get("request_id") != rid:
            return {"status": "refused", "reason": "wrong_request"}, None, ()
        tool, arguments = envelope.get("tool_name"), envelope.get("arguments", {})
        if not isinstance(tool, str) or not call_id:
            problem = "tool_name is not a string" if not isinstance(tool, str) else "the call has no ID"
            return {"status": "refused", "reason": "invalid_arguments", "error": problem}, None, ()
        decision = self.broker.begin_execute(rid, call_id, tool, arguments, thread_id, turn_id)
        if decision.kind == "refused":
            refused = {"status": "refused", "reason": decision.reason}
            if decision.text:
                refused["error"] = decision.text
            return refused, None, ()
        if decision.kind == "uncertain":
            return {"status": "uncertain", "text": decision.text}, None, ()
        if decision.kind == "stored":
            return {"status": decision.status, "text": decision.text, "repeated": True}, None, ()
        try:
            # The request's snapshot goes with the call, so a routine run by it can use only those tools.
            result = self.executor(db, self.cfg, tool, arguments, "", redacted, redacted,
                                   language=language, quiet=quiet, request_ref=rid,
                                   allowed_tools=self.broker.allowed_tool_names(rid))
        except Exception as exc:
            debug_log(f"tool {tool} raised {type(exc).__name__}", self.log_tag)
            self.broker.record_uncertain(rid, call_id)
            return ({"status": "uncertain", "text": "The action failed part way; its outcome is uncertain."},
                    None, ())
        text = redact(result.reply_text or result.error_message or "")
        pending = None if result.success else self.store.pending_for_ref(rid)
        if pending is not None:
            if self.broker.mark_awaiting_confirmation(rid, call_id, pending.request.id):
                return {"status": "awaiting_confirmation", "text": AWAITING_TEXT}, text, ()
            # The request ended while the tool ran, so nobody will hear the question: drop it, or a
            # later "yes" could approve an action the user was never asked about.
            self.store.discard_pending(pending.request.id, "cancelled")
            debug_log("confirmation for an ended request discarded", self.log_tag)
            return {"status": "refused", "reason": "not_active"}, None, ()
        if self.broker.record_result(rid, call_id, "ok" if result.success else "error", text):
            images = tuple(getattr(result, "images", ()) or ()) if result.success else ()
            return ({"status": "ok" if result.success else "error", "text": self.broker.result_text(rid, call_id)},
                    None, images)
        return {"status": "uncertain", "text": "The request ended before the action reported."}, None, ()
