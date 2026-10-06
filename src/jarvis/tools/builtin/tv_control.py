"""Control the Roku TV on the home network: remote keys, apps, typing and status.

Registered only when ``roku_host`` is set to a private address (``registry.configure_tv_tool``). All
network behaviour is in ``jarvis.devices.roku``; this tool turns it into the strings the model sees.
Routine actions, power off included, need no confirmation. See ``devices/roku.spec.md``.
"""
from typing import Any, Dict, Optional

from ...debug import debug_log
from ...devices import roku
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

_ACTIONS = ["key", "launch", "type", "status"]


def _remember_tv(last_action: str) -> None:
    """The TV is what a follow-up such as "turn it off" is about (``memory/desktop_referents.spec.md``).

    Only the action and key name are kept, never typed text or app names."""
    try:
        from ...memory.desktop_referents import get_desktop_referents
        get_desktop_referents().record_device("tv", "tvControl", last_action)
    except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
        debug_log(f"tv referent not recorded ({type(exc).__name__})", "tv")
_FORBIDDEN = ("The TV refused the command. On the Roku, go to Settings > System > Advanced system settings > "
              "Control by mobile apps > Network access and choose Default or Permissive.")
_UNREACHABLE = ("The TV did not answer. It may be off (a Roku TV only listens on the network when it is on or in "
                "standby), or its address may have changed.")
_MOVED = ("The TV had moved to a new network address and was found again. Set roku_host to its new address, or "
          "reserve one for it in the router.")
_MAX_LISTED_APPS = 30


def _fail(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, reply_text=message, error_message=message)


def _names(apps) -> str:
    return ", ".join(app.name for app in list(apps)[:_MAX_LISTED_APPS])


class TvControlTool(Tool):
    @property
    def name(self) -> str:
        return "tvControl"

    @property
    def description(self) -> str:
        # The router reads only the first 120 characters, so the TV-versus-PC pointer comes first.
        return (
            "Control the TV (Roku), NOT the PC: TV volume, mute, power, play/pause, open Netflix and other TV apps, "
            "type, status. Press remote keys (home, back, arrows, select, inputs), launch a TV app or input, type "
            "into an on-screen search box, or check what is on. For the computer's volume or music use "
            "systemVolume or mediaControl."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _ACTIONS,
                           "description": "key presses a remote key; launch opens a TV app or input by name; type "
                                          "enters text, such as a show title to search for, into the on-screen "
                                          "keyboard; status reports power, active app and installed apps."},
                "key": {"type": "string", "enum": list(roku.KEYS),
                        "description": "For key: the remote button. Play toggles play and pause; VolumeMute "
                                       "toggles mute."},
                "repeat": {"type": "integer", "minimum": 1, "maximum": roku.MAX_REPEAT,
                           "description": "OPTIONAL. Presses of the key in a row, for example volume steps."},
                "app": {"type": "string",
                        "description": "For launch: the app or input name as spoken, such as Netflix or HDMI 1."},
                "text": {"type": "string", "description": "For type: the text to enter."},
            },
            "required": ["action"],
        }

    def fast_targets(self, cfg):
        """Installed TV app names from the cache, for fast-path matching. Never touches the network."""
        from ...fastpath.matcher import FastTarget
        try:
            apps = roku.get_device(getattr(cfg, "roku_host", "")).cached_apps()
        except roku.RokuAddressError:
            return ()
        # "Tubi - Free Movies & TV" is also known as "Tubi": the text before a spaced dash is the short name.
        return tuple(FastTarget((app.name, app.name.split(" - ")[0]), app.name, app.id or "", app.name)
                     for app in apps)

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        try:
            device = roku.get_device(getattr(context.cfg, "roku_host", ""))
        except roku.RokuAddressError:
            return _fail("roku_host must be the TV's address on your home network (a private address such as "
                         "192.168.x.x).")
        debug_log(f"tvControl action={action}", "tv")
        try:
            if action == "key":
                result = self._key(device, args, context)
            elif action == "launch":
                result = self._launch(device, args, context)
            elif action == "type":
                result = self._type(device, args, context)
            elif action == "status":
                result = self._status(device, context)
            else:
                return _fail(f"Unknown action '{action}'. Use one of: {', '.join(_ACTIONS)}.")
        except roku.RokuForbidden:
            context.user_print("⚠️ The TV refused the command.")
            return _fail(_FORBIDDEN)
        except roku.RokuUnreachable:
            context.user_print("⚠️ The TV did not answer.")
            return _fail(_UNREACHABLE)
        except roku.RokuError as exc:
            context.user_print("⚠️ Could not control the TV.")
            return _fail(str(exc))
        if result.success:
            key = roku.resolve_key(args.get("key")) if action == "key" else None
            _remember_tv(f"key {key}" if key else action)
        if result.success and device.take_move_notice():
            result = ToolExecutionResult(True, f"{result.reply_text} {_MOVED}")
        return result

    def _key(self, device, args, context) -> ToolExecutionResult:
        name = roku.resolve_key(args.get("key"))
        if name is None:
            return _fail("Say which TV remote key to press, such as Home, Back, VolumeUp or PowerOff.")
        try:
            repeat = max(1, min(int(args.get("repeat") or 1), roku.MAX_REPEAT))
        except (TypeError, ValueError):
            repeat = 1
        device.call(lambda client: client.key(name, repeat))
        times = f" x{repeat}" if repeat > 1 else ""
        context.user_print(f"📺 TV key {name}{times}")
        note = " (Play toggles play and pause)" if name == "Play" else " (VolumeMute toggles mute)" if name == "VolumeMute" else ""
        return ToolExecutionResult(True, f"Sent {name}{times} to the TV{note}.")

    def _launch(self, device, args, context) -> ToolExecutionResult:
        query = str(args.get("app") or "").strip()
        if not query:
            return _fail("Say which TV app to open, such as Netflix.")

        def operation(client):
            apps = client.apps()
            device.remember_apps(apps)
            app, candidates = roku.resolve_app(query, apps)
            if app is not None:
                client.launch(app.id)
            return apps, app, candidates

        apps, app, candidates = device.call(operation)
        if app is not None:
            context.user_print(f"📺 Opened {app.name} on the TV")
            return ToolExecutionResult(True, f"Opened {app.name} on the TV.")
        if candidates:
            return _fail(f"Several TV apps match '{query}': {_names(candidates)}. Say which one.")
        return _fail(f"No TV app matches '{query}'. Installed: {_names(apps)}.")

    def _type(self, device, args, context) -> ToolExecutionResult:
        text = args.get("text")
        device.call(lambda client: client.type_text(text))
        context.user_print(f"⌨️ Typed {len(text)} characters on the TV")
        return ToolExecutionResult(True, f"Typed {len(text)} characters on the TV.")

    def _status(self, device, context) -> ToolExecutionResult:
        def operation(client):
            info = client.device_info()
            active = client.active_app()
            apps = client.apps()
            device.remember_apps(apps)
            return info, active, apps

        info, active, apps = device.call(operation)
        device.serial = info.serial or device.serial
        context.user_print(f"📺 {info.name}: {info.power_mode}")
        model = f" ({info.model})" if info.model else ""
        shown = active.name if active else "unknown"
        return ToolExecutionResult(
            True, f"TV: {info.name}{model}. Power: {info.power_mode}. Active app: {shown}. "
                  f"Installed apps: {_names(apps)}.")
