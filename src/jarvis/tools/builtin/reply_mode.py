"""Switch which model answers requests: local, ChatGPT through Codex, or Claude.

Offered only when at least one cloud mode is allowed in Settings, and never to a cloud model. The
switch itself, the allowed-mode check and persistence belong to ``bridge.modes``; this tool only
reports the outcome. See ``bridge/bridge.spec.md``.
"""
from typing import Any, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

_DISCLOSURE = {
    "local": "Everything stays on this PC.",
    "codex": "Requests now go to OpenAI through Codex.",
    "claude": "Requests now go to Anthropic through Claude Code.",
}


class ReplyModeTool(Tool):
    @property
    def name(self) -> str:
        return "replyMode"

    @property
    def description(self) -> str:
        return ("Switch which model answers Jarvis: local (on this PC), ChatGPT (Codex) or Claude, or say which "
                "one is active. NOT for opening or focusing the ChatGPT or Claude apps; use appControl.")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "get"],
                           "description": "set switches the reply mode; get reports the active one."},
                "mode": {"type": "string", "enum": ["local", "codex", "claude"],
                         "description": "For set: local, codex (ChatGPT) or claude."},
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        from ...bridge import modes

        args = args or {}
        current = modes.active_mode()
        if args.get("action") == "get":
            allowed = ", ".join(modes.LABELS[m] for m in modes.enabled_modes(context.cfg))
            return ToolExecutionResult(success=True,
                                       reply_text=f"Replies come from {modes.LABELS[current]}. Allowed: {allowed}.")
        wanted = args.get("mode")
        if args.get("action") != "set" or wanted not in modes.MODES:
            return ToolExecutionResult(success=False, reply_text=None,
                                       error_message="Say local, ChatGPT or Claude to switch the reply mode.")
        result = modes.switch(wanted)
        debug_log(f"replyMode set {wanted}: {result.reason or 'ok'}", "tools")
        label = modes.LABELS[wanted]
        if result.reason == "already":
            return ToolExecutionResult(success=True, reply_text=f"Already using {label}.")
        if result.ok:
            return ToolExecutionResult(success=True, reply_text=f"Switched to {label}. {_DISCLOSURE[wanted]}")
        if result.reason == "not_enabled":
            message = (f"{label} replies are not allowed in Settings, so I am still using "
                       f"{modes.LABELS[result.mode]}.")
        else:
            message = f"{label} replies could not start, so I am using {modes.LABELS[result.mode]}."
        return ToolExecutionResult(success=False, reply_text=None, error_message=message)
