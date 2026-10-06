"""What the user was doing on this PC in the past, from the opt-in activity log.

Read only. It returns raw aggregates (time per application and redacted window title, or ordered
sessions) for an ISO 8601 range; the reply model writes the answer. Offered only when the user has
turned the activity log on, and to Codex and Claude reply modes only when they have also allowed
sharing it. See ``memory/activity_log.spec.md``.
"""
import json
import time
from typing import Any, Dict, Optional

from ...debug import debug_log
from ...memory import activity_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


class ActivityLogTool(Tool):
    # Its results carry outside content (routines.spec.md, Prompt-injection boundary).
    returns_outside_content = True

    @property
    def name(self) -> str:
        return "activityLog"

    @property
    def description(self) -> str:
        return ("Read what the user was doing on this PC in the past: time per app and window title (summary) "
                "or an ordered timeline. Needs ISO 8601 start and end in local time. NOT for what is open "
                "right now (use windowControl), CPU or RAM use (use systemInfo), or things said in "
                "conversation (use memory).")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["summary", "timeline"],
                           "description": "summary totals time per app and window title; timeline lists "
                                          "the sessions in order."},
                "start": {"type": "string", "description": "Range start, ISO 8601 (for example "
                                                           "2026-10-03T12:00:00). Local time unless it has an offset."},
                "end": {"type": "string", "description": "Range end, ISO 8601. Defaults to now."},
                "limit": {"type": "integer", "description": "Timeline only: most sessions to return."},
            },
            "required": ["action", "start"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}

        def fail(message: str) -> ToolExecutionResult:
            return ToolExecutionResult(success=False, reply_text=None, error_message=message)

        action = args.get("action")
        if action not in ("summary", "timeline"):
            return fail("Use action summary or timeline.")
        try:
            start = activity_log.parse_time(args.get("start", ""))
            end = activity_log.parse_time(args["end"]) if args.get("end") else time.time()
        except ValueError:
            return fail("start and end must be ISO 8601 dates or times, for example 2026-10-03T12:00:00.")
        if end <= start:
            return fail("end must be after start.")

        store = activity_log.ActivityStore(context.cfg.db_path)
        try:
            if action == "summary":
                result = activity_log.summarise(store, start, end)
                count = len(result["apps"])
            else:
                limit = args.get("limit")
                result = activity_log.timeline(
                    store, start, end, limit if isinstance(limit, int) else activity_log.TIMELINE_DEFAULT_LIMIT)
                count = len(result["sessions"])
        finally:
            store.close()
        # The reply quotes this data, so the turn must stay out of the diary.
        activity_log.mark_turn_private()
        debug_log(f"activityLog {action}: {count} entries", "activity")
        return ToolExecutionResult(success=True, reply_text=json.dumps(result, ensure_ascii=False))
