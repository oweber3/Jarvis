"""Switch Jarvis's personality (how it talks): Butler, Plain, Friendly or the user's custom one.

The switch, its rules and persistence belong to ``jarvis.personality``; this tool only reports the
outcome. See ``personality.spec.md``.
"""
from typing import Any, Dict, Optional

from ...debug import debug_log
from ...system_prompt import PERSONALITIES
from ..base import Tool, ToolContext
from ..types import StepNote, ToolExecutionResult


class PersonalityTool(Tool):
    @property
    def name(self) -> str:
        return "personality"

    @property
    def description(self) -> str:
        return ("Switch Jarvis's personality, how it talks: butler, plain, friendly or the user's custom one, or "
                "say which is active. NOT for the voice or language; NOT for which model answers, use replyMode.")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "get"],
                           "description": "set switches the personality; get reports the active one."},
                "name": {"type": "string", "enum": list(PERSONALITIES),
                         "description": "For set: butler (dry wit, the default), plain (shortest answers, no "
                                        "wit), friendly (warm and casual) or custom (written in Settings)."},
            },
            "required": ["action"],
        }

    def step_note(self, args: Optional[Dict[str, Any]], result: ToolExecutionResult) -> Optional[StepNote]:
        from ... import personality
        name = (args or {}).get("name")
        switched = (result.reply_text or "").startswith("Switched")
        if (args or {}).get("action") == "set" and name in personality.LABELS and switched:
            return StepNote(f"Switched to the {personality.LABELS[name]} personality")
        return None

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        from ... import personality

        args = args or {}
        labels = personality.LABELS
        choices = ", ".join(labels[name] for name in PERSONALITIES)
        if args.get("action") == "get":
            active = personality.current(context.cfg)[0]
            return ToolExecutionResult(success=True,
                                       reply_text=f"The personality is {labels[active]}. Choices: {choices}.")
        if args.get("action") != "set":
            return ToolExecutionResult(success=False, reply_text=None,
                                       error_message=f"Say which personality to use: {choices}.")
        result = personality.switch(args.get("name"), context.cfg)
        debug_log(f"personality set {args.get('name')}: {result.reason or 'ok'}", "tools")
        if result.reason == "unknown":
            return ToolExecutionResult(success=False, reply_text=None,
                                       error_message=f"There is no such personality. Choices: {choices}.")
        if result.reason == "no_custom_text":
            return ToolExecutionResult(success=False, reply_text=None, error_message=(
                "No custom personality is written yet. Describe it in Settings, on the General page, "
                f"then ask again. Still using {labels[result.name]}."))
        label = labels[result.name]
        if result.reason == "already":
            return ToolExecutionResult(success=True, reply_text=f"Already using the {label} personality.")
        reply = f"Switched to the {label} personality."
        if result.voice_paired:
            reply += " Its paired voice is used after Jarvis restarts."
        if result.reason == "not_saved":
            reply += " It could not be saved, so it lasts until Jarvis restarts."
        return ToolExecutionResult(success=True, reply_text=reply)
