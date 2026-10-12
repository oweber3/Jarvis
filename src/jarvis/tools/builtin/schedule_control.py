"""scheduleControl: set, check and cancel timers, alarms and reminders.

A thin adapter over ``jarvis.scheduler``: times arrive as tool arguments (a duration in seconds or an ISO
local time chosen by the model, or seconds from the fast path's duration slot), never parsed from words
here. Replies use the scheduler's locale phrases. See ``scheduler/scheduler.spec.md``.
"""
from __future__ import annotations

from datetime import datetime, time as clock_time, timedelta
import unicodedata
from typing import Any, Dict, List, Optional

from ...debug import debug_log
from ...utils.redact import redact
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

ACTIONS = ("set_timer", "set_reminder", "list", "cancel")
KINDS = ("timer", "reminder", "alarm")
MAX_LABEL_CHARS = 120
_UNAVAILABLE = "Timers and reminders are not available right now."


def _key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _label(value) -> str:
    label = " ".join(value.split()) if isinstance(value, str) else ""
    if len(label) > MAX_LABEL_CHARS:
        raise ValueError(f"The name or message can be at most {MAX_LABEL_CHARS} characters.")
    return label


def _seconds(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise ValueError("seconds must be a number.") from None
    if not isinstance(value, (int, float)):
        raise ValueError("seconds must be a number.")
    return float(value)


def _due_at(value: str, now: float) -> float:
    """An ISO date-time (local unless it names an offset) or a clock time (the next time it comes round)."""
    from ...scheduler.service import MAX_AHEAD_SEC
    text = value.strip().replace(" ", "T", 1) if isinstance(value, str) else ""
    if not text:
        raise ValueError("at must be an ISO time such as 14:30 or 2026-10-13T09:00.")
    current = datetime.fromtimestamp(now)
    try:
        if "T" in text or len(text) >= 10 and text[4] == "-":
            moment = datetime.fromisoformat(text.rstrip("Z") + ("+00:00" if text.endswith("Z") else ""))
            due = moment.timestamp()
        else:
            wall = clock_time.fromisoformat(text)
            moment = datetime.combine(current.date(), wall.replace(tzinfo=None))
            if moment.timestamp() <= now:
                moment += timedelta(days=1)
            due = moment.timestamp()
    except ValueError:
        raise ValueError("at must be an ISO time such as 14:30 or 2026-10-13T09:00.") from None
    if due <= now:
        raise ValueError("That time has already passed.")
    if due - now > MAX_AHEAD_SEC:
        raise ValueError("That is too far ahead; reminders can be set up to a year ahead.")
    return due


class ScheduleControlTool(Tool):
    # Reminder messages are the user's own words: the fast path logs only argument names, and the reply
    # bridges offer the tool only while long-term memory is shared.
    log_arguments = False
    personal_data = True

    @property
    def name(self) -> str:
        return "scheduleControl"

    @property
    def description(self) -> str:
        return ("Timers, alarms and reminders: set a countdown timer, an alarm or a reminder for a time, ask how "
                "long is left, list or cancel them. NOT for routines (routineControl).")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS),
                           "description": "set_timer, set_reminder (also alarms), list (also time left) or cancel."},
                "seconds": {"type": "integer",
                            "description": "set_timer: the duration in seconds. set_reminder: optional, in this "
                                           "many seconds from now."},
                "at": {"type": "string",
                       "description": "set_reminder: local time as ISO, e.g. 14:30 or 2026-10-13T09:00."},
                "label": {"type": "string",
                          "description": "Optional. Timer name (pasta) or what to remind about (call the "
                                         "dentist); omit for a plain alarm. list/cancel: which one."},
                "kind": {"type": "string", "enum": list(KINDS),
                         "description": "Optional. list/cancel only these."},
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        from ...scheduler import phrases, runtime
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
        language = phrases.language_of(context.language)
        service = runtime.get_service()
        try:
            if action not in ACTIONS:
                raise ValueError(f"Use one of: {', '.join(ACTIONS)}.")
            kind = args.get("kind")
            if kind is not None and kind not in KINDS:
                raise ValueError(f"kind must be one of: {', '.join(KINDS)}.")
            if service is None:
                return ToolExecutionResult(success=False, reply_text=None, error_message=_UNAVAILABLE)
            if action == "set_timer":
                text = self._set_timer(service, args, language)
            elif action == "set_reminder":
                text = self._set_reminder(service, args, language)
            elif action == "list":
                text = self._list(service, kind, _label(args.get("label")), language)
            else:
                text = self._cancel(service, kind, _label(args.get("label")), language)
        except ValueError as exc:
            debug_log(f"scheduleControl {action if action in ACTIONS else 'unknown'}: refused", "scheduler")
            return ToolExecutionResult(success=False, reply_text=None, error_message=redact(str(exc)))
        return ToolExecutionResult(success=True, reply_text=redact(text))

    # --- setting ----------------------------------------------------------------------------

    def _set_timer(self, service, args, language) -> str:
        from ...scheduler import phrases
        from ...scheduler.service import MAX_AHEAD_SEC
        seconds = _seconds(args.get("seconds"))
        if seconds is None or seconds < 1:
            raise ValueError("set_timer needs seconds, a duration of at least one second.")
        if seconds > MAX_AHEAD_SEC:
            raise ValueError("That timer is too long.")
        label = _label(args.get("label"))
        service.add_timer(seconds, label, language)
        duration = phrases.format_duration(seconds, language)
        if label:
            return phrases.text("timer_set", language, label=label, duration=duration)
        return phrases.text("timer_set_unnamed", language, duration=duration)

    def _set_reminder(self, service, args, language) -> str:
        from ...scheduler import phrases
        from ...scheduler.service import MAX_AHEAD_SEC
        now = service.now()
        label = _label(args.get("label"))
        seconds = _seconds(args.get("seconds"))
        if args.get("at"):
            due = _due_at(args["at"], now)
        elif seconds is not None:
            if seconds < 1 or seconds > MAX_AHEAD_SEC:
                raise ValueError("seconds must be between one second and a year.")
            due = now + seconds
        else:
            raise ValueError("set_reminder needs at (an ISO time) or seconds from now.")
        service.add_reminder(due, label, language)
        when = phrases.format_when(due, now, language)
        if label:
            return phrases.text("reminder_set", language, when=when, message=label)
        return phrases.text("alarm_set", language, when=when)

    # --- reading and cancelling -------------------------------------------------------------

    def _matching(self, service, kind, label) -> List[Any]:
        items = service.pending(kind)
        if not label:
            return items
        wanted = _key(label)
        exact = [item for item in items if _key(item.label) == wanted]
        if exact:
            return exact
        return [item for item in items if item.label and (wanted in _key(item.label) or _key(item.label) in wanted)]

    def _describe(self, item, now, language) -> str:
        from ...scheduler import phrases
        if item.kind == "timer":
            duration = phrases.format_duration(max(1.0, item.due - now), language)
            if item.label:
                return phrases.text("timer_left", language, label=item.label, duration=duration)
            return phrases.text("timer_left_unnamed", language, duration=duration)
        when = phrases.format_when(item.due, now, language)
        if item.kind == "reminder":
            return phrases.text("reminder_item", language, when=when, message=item.label)
        return phrases.text("alarm_item", language, when=when)

    def _list(self, service, kind, label, language) -> str:
        from ...scheduler import phrases
        items = self._matching(service, kind, label)
        if not items:
            if label:
                raise ValueError(phrases.text("no_match", language))
            return phrases.text(f"none_{kind}" if kind else "none_any", language)
        now = service.now()
        return "\n".join(self._describe(item, now, language) for item in items)

    def _cancel(self, service, kind, label, language) -> str:
        from ...scheduler import phrases
        items = self._matching(service, kind, label)
        if not items:
            raise ValueError(phrases.text("no_match", language))
        if len(items) > 1:
            now = service.now()
            raise ValueError(phrases.text("which_one", language,
                                          items=" ".join(self._describe(item, now, language) for item in items)))
        item = items[0]
        if not service.cancel(item.id):
            raise ValueError(phrases.text("no_match", language))
        if item.kind == "timer":
            return (phrases.text("cancelled_timer", language, label=item.label) if item.label
                    else phrases.text("cancelled_timer_unnamed", language))
        if item.kind == "reminder":
            return phrases.text("cancelled_reminder", language, message=item.label)
        return phrases.text("cancelled_alarm", language, when=phrases.format_when(item.due, service.now(), language))
