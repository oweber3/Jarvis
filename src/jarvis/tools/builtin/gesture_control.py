"""Turn the air mouse on and off: the webcam follows the user's hand and moves the pointer.

Registered only on Windows when ``air_mouse_enabled`` is on (``registry.configure_gesture_tool``), and never
offered to a cloud model. The mode itself lives in ``jarvis.gestures.air_mouse_service``; this tool only
starts, stops and reports it. See ``gestures/gestures.spec.md``, Air mouse.
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
        return ("Air mouse (gesture control): start or stop moving the mouse pointer with your hand through the "
                "webcam, or say if it is on. Pinch to click, hold the pinch to drag. NOT for clicking or typing "
                "on Jarvis's behalf; use uiControl.")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _ACTIONS,
                           "description": "start turns the air mouse on (camera on); stop turns it off (camera "
                                          "off); status says whether it is on."},
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        from ...gestures import air_mouse_service

        action = str((args or {}).get("action") or "").strip().lower()
        service = air_mouse_service.get_service()
        if action not in _ACTIONS:
            return _fail(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")
        if service is None:
            return _fail("The air mouse is turned off in Settings (Air Mouse).")
        debug_log(f"gestureControl action={action}", "gestures")
        if action == "status":
            on = service.running
            return ToolExecutionResult(True, "The air mouse is on." if on else "The air mouse is off.")
        if action == "stop":
            service.stop()
            return ToolExecutionResult(True, "The air mouse is off and the camera is released.")
        try:
            outcome = service.start()
        except air_mouse_service.AirMouseUnavailable as exc:
            context.user_print(f"⚠️ The air mouse could not start: {exc}")
            return _fail(f"The air mouse could not start: {exc}.")
        if outcome == "already":
            return ToolExecutionResult(True, "The air mouse is already on.")
        return ToolExecutionResult(True, f"The air mouse is on. {air_mouse_service.GESTURE_GUIDE}")
