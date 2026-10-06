"""Read and change the Windows system volume."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ....debug import debug_log
from ....platform.windows import audio
from ...base import Tool, ToolContext
from ...types import ToolExecutionResult
from ._common import disabled_result, failure, parse_number

_ACTIONS = ["get", "set", "up", "down", "mute", "unmute"]
_DEFAULT_STEP = 10


def _describe(state: audio.VolumeState) -> str:
    return f"{state.percent}%" + (" (muted)" if state.muted else "")


class SystemVolumeTool(Tool):
    """Get or set the master volume of the default playback device."""

    @property
    def name(self) -> str:
        return "systemVolume"

    @property
    def description(self) -> str:
        return (
            "Read or change the computer's master audio volume: get the current "
            "level, set it to a percentage, turn it up or down, mute or unmute "
            "(e.g. 'set volume to 30%', 'turn it up', 'mute'). Controls the "
            "system output device, not any single app. NOT for pausing or "
            "skipping music; that is mediaControl."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": _ACTIONS,
                    "description": "get, set (needs percent), up, down, mute or unmute.",
                },
                "percent": {
                    "type": "number",
                    "description": "Target volume 0 to 100. Required for action 'set'.",
                },
                "amount": {
                    "type": "number",
                    "description": (
                        f"OPTIONAL. Percentage points to change for 'up' or 'down' "
                        f"(default {_DEFAULT_STEP})."
                    ),
                },
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        blocked = disabled_result(context)
        if blocked:
            return blocked

        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        debug_log(f"systemVolume action={action}", "windows")

        try:
            if action == "get":
                state = audio.get_volume()
                context.user_print(f"🔊 Volume is {_describe(state)}")
                return ToolExecutionResult(True, f"System volume: {_describe(state)}")

            if action == "set":
                percent = parse_number(args.get("percent"))
                if percent is None:
                    return failure("A volume percentage between 0 and 100 is required.")
                target = round(percent)
                if not 0 <= target <= 100:
                    return failure(f"Volume must be between 0 and 100, not {target}.")
                state = audio.set_volume(target)
                context.user_print(f"🔊 Volume set to {_describe(state)}")
                return ToolExecutionResult(True, f"System volume set to {_describe(state)}")

            if action in ("up", "down"):
                amount = parse_number(args.get("amount"))
                step = round(amount) if amount is not None and amount > 0 else _DEFAULT_STEP
                state = audio.change_volume(step if action == "up" else -step)
                verb = "increased" if action == "up" else "decreased"
                context.user_print(f"🔊 Volume {verb} to {_describe(state)}")
                return ToolExecutionResult(True, f"System volume {verb} to {_describe(state)}")

            if action in ("mute", "unmute"):
                state = audio.set_muted(action == "mute")
                icon = "🔇" if state.muted else "🔊"
                context.user_print(f"{icon} Audio {'muted' if state.muted else 'unmuted'}")
                return ToolExecutionResult(
                    True, f"System audio {'muted' if state.muted else 'unmuted'}; volume {_describe(state)}"
                )
        except audio.AudioError as exc:
            context.user_print("⚠️ Could not control the volume.")
            return failure(str(exc))

        return failure(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")
