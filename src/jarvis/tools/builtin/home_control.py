"""Control the user's smart home devices: lights, plugs, fans, blinds, thermostats, locks, alarms and sensors.

Registered only while ``smarthome_enabled`` is true (``registry.configure_home_tool``). Devices come from
the user's own Home Assistant and MQTT broker (``jarvis.smarthome``); this tool turns them into raw facts
for the model. Device names and states are untrusted text from the hub and are fenced as data. Unlocking,
disarming, opening garage doors and gates, scripts, buttons, extreme thermostat settings and changes to more
than 10 devices confirm by voice. See ``smarthome/smarthome.spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...debug import debug_log
from ...smarthome import service
from ...smarthome.model import (KINDS, LEVEL_PROPERTIES, SET_PROPERTIES, Command, Device, Resolution, comfortable,
                                parse_colour, supports)
from ..base import Tool, ToolContext
from ..types import StepNote, ToolExecutionResult
from .web_fence import fence_json

ACTIONS = ["list", "status", "turn_on", "turn_off", "toggle", "set", "open", "close", "stop", "lock", "unlock",
           "arm", "disarm", "activate"]
READ_ACTIONS = ("list", "status")
MAX_AT_ONCE = 10
MAX_LISTED = 60
MAX_RECORDED_REFERENTS = 10
RISKY_COVER_CLASSES = ("garage", "gate", "door")
_TAG = "smarthome"
_SET_ORDER = ("brightness", "colour", "colour_temperature", "speed", "position", "temperature", "mode", "level")
_RANGES = {"brightness": (0, 100), "speed": (0, 100), "position": (0, 100), "level": (0, 100),
           "colour_temperature": (1500, 9000), "temperature": (-50, 150)}
_NOT_RUNNING = ("Smart home is not connected. Turn it on in Settings > Smart Home and connect Home Assistant or an "
                "MQTT broker.")
# Plural nouns for step notes in the web chat (English, like every other step note).
_KIND_WORDS = {"light": ("light", "lights"), "switch": ("switch", "switches"), "fan": ("fan", "fans"),
               "cover": ("blind", "blinds"), "climate": ("thermostat", "thermostats"), "lock": ("lock", "locks"),
               "alarm": ("alarm", "alarms"), "scene": ("scene", "scenes"), "script": ("script", "scripts"),
               "button": ("button", "buttons")}
_VERBS = {"turn_on": "Turned on", "turn_off": "Turned off", "toggle": "Toggled", "open": "Opened",
          "close": "Closed", "stop": "Stopped", "lock": "Locked", "unlock": "Unlocked", "arm": "Armed",
          "disarm": "Disarmed", "activate": "Activated"}


@dataclass
class HomeResult(ToolExecutionResult):
    """A result that also carries what changed, for the step note (kinds and areas only)."""
    changed: Tuple[Tuple[str, str], ...] = ()   # (kind, area) per device changed


def _fail(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, reply_text=message, error_message=message)


def _fact(dev: Device) -> Dict[str, Any]:
    fact: Dict[str, Any] = {"name": dev.name, "kind": dev.kind, "state": dev.state}
    if dev.area:
        fact["area"] = dev.area
    if dev.device_class:
        fact["class"] = dev.device_class
    attributes = dict(dev.attributes)
    if "brightness" in attributes and isinstance(attributes["brightness"], (int, float)):
        fact["brightness_percent"] = round(attributes["brightness"] * 100 / 255)
    for key in ("brightness_percent", "current_position", "temperature", "current_temperature", "percentage",
                "unit_of_measurement", "temperature_unit", "hvac_modes", "battery_level"):
        if key in attributes and key not in fact:
            fact[key] = attributes[key]
    return fact


def _facts(devices: Sequence[Device]) -> str:
    listed = [_fact(d) for d in list(devices)[:MAX_LISTED]]
    more = len(devices) - len(listed)
    text = fence_json(listed, "home")
    return text + (f"\n(and {more} more not listed)" if more > 0 else "")


def _set_property(args: Dict[str, Any]) -> Tuple[str, Any]:
    for prop in _SET_ORDER:
        if args.get(prop) not in (None, ""):
            return prop, args[prop]
    return "", None


def _value_problem(prop: str, value: Any) -> Optional[str]:
    if prop == "colour":
        return None if parse_colour(value) is not None else "Give the colour as a CSS colour name or #RRGGBB."
    if prop == "mode":
        return None if isinstance(value, str) and 0 < len(value) <= 30 else "Give the mode, such as heat or off."
    low, high = _RANGES[prop]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        return f"{prop.replace('_', ' ').capitalize()} must be a number from {low} to {high}."
    return None


def _device_property(dev: Device, prop: str) -> str:
    """The property ``level`` means for this device."""
    if prop != "level":
        return prop
    return next((p for p in LEVEL_PROPERTIES if SET_PROPERTIES[p] in dev.capabilities), "")


def _targets(home, args: Dict[str, Any]) -> Tuple[Resolution, List[Device]]:
    """The resolution for the request and the devices in it that can do the action."""
    action = str(args.get("action") or "").strip().lower()
    kind = str(args.get("kind") or "").strip().lower()
    kind = kind if kind in KINDS else ""
    resolution = home.registry.resolve(str(args.get("target") or ""), str(args.get("area") or ""), kind)
    prop, _value = _set_property(args)
    able = [d for d in resolution.devices if supports(d, action, prop)]
    return resolution, able


def _names(devices: Sequence[Device], limit: int = 3) -> str:
    names = [d.name for d in devices[:limit]]
    rest = len(devices) - len(names)
    return ", ".join(names) + (f" and {rest} more" if rest > 0 else "")


class HomeControlTool(Tool):
    # Device names and states come from the hub, outside Jarvis.
    returns_outside_content = True

    @property
    def name(self) -> str:
        return "homeControl"

    @property
    def description(self) -> str:
        # The router reads only the first 120 characters, so home devices and the TV pointer come first.
        return (
            "Control smart home devices: lights, plugs, fans, blinds, thermostats, locks, alarms, sensors, scenes. "
            "NOT the TV (use tvControl) or this PC's volume or music (systemVolume, mediaControl). List devices, "
            "read a state (is the garage door open, the bedroom temperature), turn on or off, dim, set a colour, "
            "fan speed, blind position or temperature, open, close, lock, arm, run a scene."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ACTIONS,
                           "description": "list names devices; status reads states; turn_on, turn_off and toggle "
                                          "are for lights, plugs and fans; set changes one property; open, close "
                                          "and stop are for blinds, curtains, garage doors and gates; lock and "
                                          "unlock; arm and disarm an alarm; activate runs a scene, script or "
                                          "button."},
                "target": {"type": "string",
                           "description": "OPTIONAL. The device's name as the user says it, without words like "
                                          "'the'. Leave out when the user names only a room and a kind."},
                "area": {"type": "string", "description": "OPTIONAL. The room, such as kitchen or bedroom."},
                "kind": {"type": "string", "enum": list(KINDS),
                         "description": "OPTIONAL. The kind of device: light for lights and lamps, switch for plugs, "
                                        "cover for blinds and garage doors, climate for thermostats."},
                "brightness": {"type": "integer", "minimum": 0, "maximum": 100,
                               "description": "OPTIONAL. For set: light brightness in percent."},
                "colour": {"type": "string",
                           "description": "OPTIONAL. For set: a CSS colour name in English (red, warmwhite) or "
                                          "#RRGGBB."},
                "colour_temperature": {"type": "integer", "minimum": 1500, "maximum": 9000,
                                       "description": "OPTIONAL. For set: white tone in kelvin, 2700 warm to 6500 "
                                                      "cool."},
                "speed": {"type": "integer", "minimum": 0, "maximum": 100,
                          "description": "OPTIONAL. For set: fan speed in percent."},
                "position": {"type": "integer", "minimum": 0, "maximum": 100,
                             "description": "OPTIONAL. For set: blind or cover position, 0 closed to 100 open."},
                "temperature": {"type": "number",
                                "description": "OPTIONAL. For set: thermostat target in the thermostat's own unit."},
                "mode": {"type": "string", "description": "OPTIONAL. For set: thermostat mode such as heat, cool, "
                                                          "auto or off."},
                "level": {"type": "integer", "minimum": 0, "maximum": 100,
                          "description": "OPTIONAL. For set: a percentage for whichever of brightness, fan speed or "
                                         "position the device has."},
            },
            "required": ["action"],
        }

    # -- safety --------------------------------------------------------------------------------

    def classify_safety(self, args, cfg):
        from ..confirmation import ConfirmationRequest, SafetyTier
        args = args if isinstance(args, dict) else {}
        action = str(args.get("action") or "").strip().lower()
        request = ConfirmationRequest(tool_name=self.name, tier=SafetyTier.SAFE, action=f"home {action}",
                                      target="", parameters=dict(args))
        home = service.get_home()
        if action in READ_ACTIONS or action not in ACTIONS or home is None:
            return request
        _resolution, devices = _targets(home, args)
        if not devices:
            return request  # nothing would change; running says why
        reason = self._risk(action, args, devices, cfg)
        if reason:
            request.tier = SafetyTier.CONFIRM_VOICE
            request.target = _names(devices)
            request.consequence = reason
            debug_log(f"homeControl {action} needs confirmation ({len(devices)} devices)", _TAG)
        return request

    @staticmethod
    def _risk(action: str, args: Dict[str, Any], devices: Sequence[Device], cfg) -> Optional[str]:
        if len(devices) > MAX_AT_ONCE:
            return f"{len(devices)} devices will change at once."
        always = set(getattr(cfg, "smarthome_confirm_devices", None) or [])
        prop, value = _set_property(args)
        for dev in devices:
            if dev.id in always:
                return "You asked Jarvis to always check before changing this device."
            if action == "unlock":
                return "The lock will be unlocked."
            if action == "disarm":
                return "The alarm will be disarmed."
            opening = action == "open" or (action == "set" and _device_property(dev, prop) == "position")
            if dev.kind == "cover" and dev.device_class in RISKY_COVER_CLASSES and opening:
                return f"The {dev.device_class} will open."
            if (dev.kind == "climate" and action == "set" and prop == "temperature"
                    and isinstance(value, (int, float)) and not isinstance(value, bool)
                    and not comfortable(value, dev.attributes.get("temperature_unit", ""))):
                return "The thermostat will be set outside the usual range."
            if dev.kind in ("script", "button") and action == "activate":
                return f"The {dev.kind} will run, and Jarvis does not know what it does."
        return None

    # -- run -----------------------------------------------------------------------------------

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        home = service.get_home()
        if home is None:
            return _fail(_NOT_RUNNING)
        if action not in ACTIONS:
            return _fail(f"Unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.")
        if action == "list":
            return self._list(home, args, context)
        resolution, devices = _targets(home, args)
        debug_log(f"homeControl action={action} resolved={len(resolution.devices)} "
                  f"candidates={len(resolution.candidates)} able={len(devices)}", _TAG)
        if action == "status":
            found = resolution.devices or resolution.candidates
            if not found:
                return _fail("No device Jarvis can see matches that. Devices must be exposed to voice assistants in "
                             "Home Assistant.")
            context.user_print(f"🏠 Read {len(found)} device states")
            return ToolExecutionResult(True, f"Current states:\n{_facts(found)}")
        if not resolution.devices:
            if resolution.candidates:
                return _fail("Several devices match, so nothing was changed. Ask which one, or name the room. "
                             f"Matching devices:\n{_facts(resolution.candidates)}")
            return _fail("No device Jarvis can see matches that, so nothing was changed. Devices must be exposed to "
                         "voice assistants in Home Assistant.")
        if not devices:
            return _fail(f"None of these devices can do {action.replace('_', ' ')}, so nothing was changed:\n"
                         f"{_facts(resolution.devices)}")
        command, problem = self._command(action, args, devices)
        if problem:
            return _fail(problem)
        return self._execute(home, action, devices, command, context)

    def _list(self, home, args, context) -> ToolExecutionResult:
        kind = str(args.get("kind") or "").strip().lower()
        resolution = home.registry.resolve(str(args.get("target") or ""), str(args.get("area") or ""),
                                           kind if kind in KINDS else "")
        found = resolution.devices or resolution.candidates
        context.user_print(f"🏠 {len(found)} smart home devices")
        if not found:
            return ToolExecutionResult(True, "No devices match. Devices must be exposed to voice assistants in "
                                             "Home Assistant, or announced on the MQTT broker.")
        return ToolExecutionResult(True, f"{len(found)} devices:\n{_facts(found)}")

    @staticmethod
    def _command(action: str, args: Dict[str, Any], devices: Sequence[Device]):
        if action != "set":
            return {d.id: Command(action) for d in devices}, None
        prop, value = _set_property(args)
        if not prop:
            return None, ("Say what to set: brightness, colour, colour_temperature, speed, position, temperature "
                          "or mode.")
        problem = _value_problem(prop, value)
        if problem:
            return None, problem
        commands = {}
        for dev in devices:
            own = _device_property(dev, prop)
            commands[dev.id] = Command("set", own, int(value) if own in _RANGES and own != "temperature" else value)
        return commands, None

    def _execute(self, home, action, devices, commands, context) -> ToolExecutionResult:
        done: List[Device] = []
        unconfirmed: List[Tuple[Device, str]] = []
        failed: List[Tuple[Device, str]] = []
        for dev in devices:
            try:
                outcome = home.send(dev, commands[dev.id])
            except service.SmartHomeError as exc:
                failed.append((dev, str(exc)))
                continue
            if outcome.confirmed:
                done.append(dev)
            else:
                unconfirmed.append((dev, outcome.note))
        acted = done + [d for d, _note in unconfirmed]
        kinds = ",".join(sorted({d.kind for d in devices}))
        debug_log(f"homeControl {action} ({kinds}): {len(done)} confirmed, {len(unconfirmed)} unconfirmed, "
                  f"{len(failed)} failed", _TAG)
        if acted:
            self._remember(acted, action)
        lines = []
        refreshed = [home.registry.get(d.id) or d for d in done]
        if done:
            lines.append(f"Done and confirmed by the hub for {len(done)} devices:\n{_facts(refreshed)}")
        for dev, note in unconfirmed:
            lines.append(f"{note}\n{_facts([dev])}")
        for dev, reason in failed:
            lines.append(f"Not changed: {reason}\n{_facts([dev])}")
        if not done:
            # Nothing the hub confirmed is reported as done, so a fast-path reply never claims it.
            context.user_print("⚠️ The smart home command was not confirmed" if unconfirmed
                               else "⚠️ The smart home command failed")
            reason = unconfirmed[0][1] if unconfirmed else failed[0][1]
            return ToolExecutionResult(False, "\n".join(lines), error_message=reason)
        context.user_print(f"🏠 {_VERBS.get(action, 'Changed')} {len(acted)} devices")
        return HomeResult(True, "\n".join(lines), changed=tuple((d.kind, d.area) for d in done))

    @staticmethod
    def _remember(devices: Sequence[Device], action: str) -> None:
        """Follow-ups such as "turn them off" reach the same devices (memory/desktop_referents.spec.md).
        Only device ids are recorded."""
        try:
            from ...memory.desktop_referents import get_desktop_referents
            referents = get_desktop_referents()
            for dev in list(devices)[:MAX_RECORDED_REFERENTS]:
                referents.record_device(dev.id, "homeControl", action)
        except Exception as exc:  # noqa: BLE001 - remembering never changes an action's result
            debug_log(f"home referent not recorded ({type(exc).__name__})", _TAG)

    # -- web chat and fast path -----------------------------------------------------------------

    def step_note(self, args: Optional[Dict[str, Any]], result: ToolExecutionResult) -> Optional[StepNote]:
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        changed = getattr(result, "changed", ())
        if action in READ_ACTIONS:
            return StepNote("Checked smart home devices")
        if not changed:
            return None
        kinds = {kind for kind, _area in changed}
        areas = {area for _kind, area in changed if area}
        if action == "set":
            prop, _value = _set_property(args)
            prop = prop.replace("_", " ") if prop != "level" else "level"
            place = f"the {next(iter(areas))} " if len(areas) == 1 else "the "
            return StepNote(f"Set {place}{prop}")
        count = len(changed)
        if len(kinds) == 1:
            singular, plural = _KIND_WORDS.get(next(iter(kinds)), ("device", "devices"))
            noun = singular if count == 1 else plural
        else:
            noun = "device" if count == 1 else "devices"
        return StepNote(f"{_VERBS.get(action, 'Changed')} {count} {noun}")

    def fast_targets(self, cfg):
        """Device names and areas from memory, for fast-path matching. Never touches the network."""
        from ...fastpath.matcher import FastTarget
        home = service.get_home()
        if home is None:
            return (), ()
        devices = tuple(FastTarget((d.name, *d.aliases), d.id, d.kind, d.name) for d in home.registry.devices())
        areas = tuple(FastTarget((area,), area, "area", area) for area in home.registry.areas())
        return devices, areas
