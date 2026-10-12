"""Turn gesture snapping on and off: the webcam follows the user's hand, a fist grabs a window and opening it
snaps the window into a FancyZones zone; a pinch scrolls.

Registered only on Windows when ``gesture_snap_enabled`` is on (``registry.configure_gesture_tool``), and never
offered to a cloud model. The mode itself lives in ``jarvis.gestures.window_snap_service``; this tool only
starts, stops and reports it. See ``gestures/gestures.spec.md``, Gesture snapping.
"""
from typing import Any, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

_ACTIONS = ["start", "stop", "status"]


def _fail(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, reply_text=message, error_message=message)


class GestureControlTool(Tool):
    @property
    def name(self) -> str:
        return "gestureControl"

    @property
    def description(self) -> str:
        # The router reads only the first 120 characters.
        return ("Gesture control (gesture snapping): start or stop moving windows into zones with your hand through "
                "the webcam, or say if it is on. A fist grabs the window under the pointer, a pinch scrolls. NOT "
                "for placing a named window by voice; use windowControl.")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _ACTIONS,
                           "description": "start turns gesture snapping on (camera on); stop turns it off (camera "
                                          "off); status says whether it is on."},
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        from ...gestures import window_snap_service

        action = str((args or {}).get("action") or "").strip().lower()
        service = window_snap_service.get_service()
        if action not in _ACTIONS:
            return _fail(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")
        if service is None:
            return _fail("Gesture snapping is turned off in Settings (Gesture Snapping).")
        debug_log(f"gestureControl action={action}", "gestures")
        if action == "status":
            on = service.running
            return ToolExecutionResult(True, "Gesture snapping is on." if on else "Gesture snapping is off.")
        if action == "stop":
            service.stop()
            return ToolExecutionResult(True, "Gesture snapping is off and the camera is released.")
        try:
            outcome = service.start()
        except window_snap_service.WindowSnapUnavailable as exc:
            context.user_print(f"⚠️ Gesture snapping could not start: {exc}")
            return _fail(f"Gesture snapping could not start: {exc}.")
        if outcome == "already":
            return ToolExecutionResult(True, "Gesture snapping is already on.")
        return ToolExecutionResult(True, f"Gesture snapping is on. {window_snap_service.GESTURE_GUIDE}")
