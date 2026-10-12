"""Turn gesture snapping on and off: the webcam counts the user's raised fingers; one finger aims, a fist grabs a
window and raising a finger again snaps it into a FancyZones zone; two fingers scroll. While it is on, the click
actions click at the pointer the user's hand placed (a spoken "click").

Registered only on Windows when ``gesture_snap_enabled`` is on (``registry.configure_gesture_tool``), and never
offered to a cloud model; a request from a paired phone may stop it but not start it or click. The mode itself lives in ``jarvis.gestures.window_snap_service``; this tool only
starts, stops and reports it. See ``gestures/gestures.spec.md``, Gesture snapping.
"""
from typing import Any, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

_CLICKS = ["click", "double_click", "right_click"]
_ACTIONS = ["start", "stop", "status", *_CLICKS]


def _fail(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, reply_text=message, error_message=message)


class GestureControlTool(Tool):
    @property
    def name(self) -> str:
        return "gestureControl"

    @property
    def description(self) -> str:
        # The router reads only the first 120 characters.
        return ("Gesture control (gesture snapping): start, stop or check hand control of windows via webcam, or "
                "click where my hand points. A fist grabs the window under the pointer, two fingers scroll; click, "
                "double click or right click at the pointer while it is on. NOT for clicking a named button "
                "(uiControl) or placing a named window by voice (windowControl).")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _ACTIONS,
                           "description": "start turns gesture snapping on (camera on); stop turns it off (camera "
                                          "off); status says whether it is on; click, double_click and right_click "
                                          "click at the pointer while it is on."},
            },
            "required": ["action"],
        }

    def fast_available(self, cfg: Any, args: Dict[str, Any]) -> bool:
        """Spoken clicks are fast-routed only while gesture snapping runs; with it off the words go to the model."""
        if str((args or {}).get("action") or "") not in _CLICKS:
            return True
        from ...gestures import window_snap_service
        service = window_snap_service.get_service()
        return service is not None and service.running

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        from ...gestures import window_snap_service
        from ..request_scope import from_phone

        action = str((args or {}).get("action") or "").strip().lower()
        service = window_snap_service.get_service()
        if action not in _ACTIONS:
            return _fail(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")
        if service is None:
            return _fail("Gesture snapping is turned off in Settings (Extras, Gesture control).")
        debug_log(f"gestureControl action={action}", "gestures")
        if action == "status":
            on = service.running
            return ToolExecutionResult(True, "Gesture snapping is on." if on else "Gesture snapping is off.")
        if action == "stop":
            service.stop()
            return ToolExecutionResult(True, "Gesture snapping is off and the camera is released.")
        if action in _CLICKS:
            return self._click(service, action)
        if from_phone():
            debug_log("gestureControl start refused: the request came from a phone", "gestures")
            return _fail("Gesture snapping turns the webcam on, so it can only be started at the PC.")
        try:
            outcome = service.start()
        except window_snap_service.WindowSnapUnavailable as exc:
            context.user_print(f"⚠️ Gesture snapping could not start: {exc}")
            return _fail(f"Gesture snapping could not start: {exc}.")
        if outcome == "already":
            return ToolExecutionResult(True, "Gesture snapping is already on.")
        return ToolExecutionResult(True, f"Gesture snapping is on. {window_snap_service.GESTURE_GUIDE}")

    @staticmethod
    def _click(service: Any, action: str) -> ToolExecutionResult:
        from ..request_scope import from_phone
        if from_phone():
            debug_log("gestureControl click refused: the request came from a phone", "gestures")
            return _fail("Clicking by voice works only at the PC.")
        try:
            outcome = service.click(action)
        except OSError as exc:
            debug_log(f"gestureControl click refused by Windows ({type(exc).__name__})", "gestures")
            return _fail("Windows blocked the click.")
        if outcome == "off":
            return _fail("Clicking by voice works only while gesture control is on.")
        if outcome == "busy":
            return _fail("Let go of the window or finish scrolling first.")
        return ToolExecutionResult(True, "Clicked.")
