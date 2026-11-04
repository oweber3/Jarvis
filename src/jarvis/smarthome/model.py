"""The shared smart home device model: ``Device``, ``Capability``, the registry and name resolution.

No network code and no knowledge of connectors, tools or the LLM. Connectors fill the registry; the tool
and the fast path read it. Words are compared after Unicode case folding, never against a list of words
in one language. See ``smarthome.spec.md``.
"""
from __future__ import annotations

import dataclasses
import enum
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from ..debug import debug_log

MAX_DEVICES = 2000
MAX_NAME_CHARS = 100
MAX_STATE_CHARS = 100
MAX_ATTRIBUTES = 40
UNAVAILABLE = "unavailable"

KINDS = ("light", "switch", "fan", "cover", "climate", "lock", "alarm", "sensor", "binary_sensor", "scene",
         "script", "button")
# Kinds whose devices never change: they are only read.
READ_ONLY_KINDS = ("sensor", "binary_sensor")


class Capability(str, enum.Enum):
    ON_OFF = "on_off"
    BRIGHTNESS = "brightness"
    COLOUR = "colour"
    COLOUR_TEMPERATURE = "colour_temperature"
    SPEED = "speed"
    OPEN_CLOSE = "open_close"
    STOP = "stop"
    POSITION = "position"
    TARGET_TEMPERATURE = "temperature"
    MODE = "mode"
    LOCK = "lock"
    ARM = "arm"
    READ = "read"
    ACTIVATE = "activate"


_KIND_CAPABILITIES: Dict[str, frozenset] = {
    "light": frozenset({Capability.ON_OFF, Capability.BRIGHTNESS, Capability.COLOUR,
                        Capability.COLOUR_TEMPERATURE}),
    "switch": frozenset({Capability.ON_OFF}),
    "fan": frozenset({Capability.ON_OFF, Capability.SPEED}),
    "cover": frozenset({Capability.OPEN_CLOSE, Capability.STOP, Capability.POSITION}),
    "climate": frozenset({Capability.TARGET_TEMPERATURE, Capability.MODE}),
    "lock": frozenset({Capability.LOCK}),
    "alarm": frozenset({Capability.ARM}),
    "sensor": frozenset({Capability.READ}),
    "binary_sensor": frozenset({Capability.READ}),
    "scene": frozenset({Capability.ACTIVATE}),
    "script": frozenset({Capability.ACTIVATE}),
    "button": frozenset({Capability.ACTIVATE}),
}

# The tool's actions and the capability each needs. ``set`` depends on the property being set.
ACTION_CAPABILITY: Dict[str, Capability] = {
    "turn_on": Capability.ON_OFF, "turn_off": Capability.ON_OFF, "toggle": Capability.ON_OFF,
    "open": Capability.OPEN_CLOSE, "close": Capability.OPEN_CLOSE, "stop": Capability.STOP,
    "lock": Capability.LOCK, "unlock": Capability.LOCK,
    "arm": Capability.ARM, "disarm": Capability.ARM,
    "activate": Capability.ACTIVATE,
}
SET_PROPERTIES: Dict[str, Capability] = {
    "brightness": Capability.BRIGHTNESS, "colour": Capability.COLOUR,
    "colour_temperature": Capability.COLOUR_TEMPERATURE, "speed": Capability.SPEED,
    "position": Capability.POSITION, "temperature": Capability.TARGET_TEMPERATURE, "mode": Capability.MODE,
}
# ``level`` is a percentage for whichever of these the device has (the fast path's "set X to 50 percent").
LEVEL_PROPERTIES = ("brightness", "speed", "position")


def capabilities_for(kind: str, narrowed: Optional[Iterable[Capability]] = None) -> frozenset:
    """The capabilities of a kind, narrowed to what the hub says the device supports (never widened)."""
    full = _KIND_CAPABILITIES.get(kind, frozenset())
    return full if narrowed is None else full & frozenset(narrowed)


@dataclass(frozen=True)
class Device:
    id: str                  # ha:<entity_id> or mqtt:<unique_id>
    name: str
    kind: str
    source: str              # ha | mqtt
    area: str = ""
    aliases: Tuple[str, ...] = ()
    state: str = ""
    capabilities: frozenset = frozenset()
    # Hub facts the tool reports or needs: brightness, position, temperature, unit, device_class, ...
    attributes: Mapping[str, Any] = field(default_factory=dict)
    device_class: str = ""
    # The hub's unique id, shared by both copies of an MQTT device that Home Assistant also has.
    unique_id: str = ""


def supports(device: Device, action: str, prop: str = "") -> bool:
    """Whether ``device`` can do ``action`` (for ``set``: set ``prop``)."""
    if action in ("list", "status"):
        return True
    if action == "set":
        if prop == "level":
            return any(SET_PROPERTIES[p] in device.capabilities for p in LEVEL_PROPERTIES)
        capability = SET_PROPERTIES.get(prop)
        return capability is not None and capability in device.capabilities
    capability = ACTION_CAPABILITY.get(action)
    return capability is not None and capability in device.capabilities


def tokens(text: str) -> Tuple[str, ...]:
    """Words of ``text`` after Unicode case folding, in any script."""
    return tuple(re.findall(r"\w+", str(text or "").casefold(), re.UNICODE))


def pick(query: str, entries: Sequence[Tuple[Sequence[str], Any]]) -> Tuple[Tuple[Any, ...], Tuple[Any, ...]]:
    """Exact-then-unique choice among ``(names, value)`` entries.

    Returns ``((value,), ())`` for an exact name or one entry whose name holds every spoken word,
    ``((), candidates)`` when several fit and ``((), ())`` when none do. Never picks among several."""
    wanted = tokens(query)
    if not wanted:
        return (), ()
    folded = str(query).strip().casefold()
    exact = _unique(value for names, value in entries
                    if any(tokens(name) == wanted or str(name).casefold() == folded for name in names))
    if len(exact) == 1:
        return exact, ()
    if exact:
        return (), exact
    needed = set(wanted)
    partial = _unique(value for names, value in entries if any(needed <= set(tokens(name)) for name in names))
    if len(partial) == 1:
        return partial, ()
    return (), partial


def _unique(values: Iterable[Any]) -> Tuple[Any, ...]:
    seen, kept = set(), []
    for value in values:
        key = id(value) if not isinstance(value, (str, int)) else value
        if key not in seen:
            seen.add(key)
            kept.append(value)
    return tuple(kept)


@dataclass(frozen=True)
class Resolution:
    """The devices a request reaches, or the candidates when it is ambiguous (nothing is changed then)."""
    devices: Tuple[Device, ...] = ()
    candidates: Tuple[Device, ...] = ()


def _bounded(device: Device) -> Device:
    attributes = dict(list(dict(device.attributes or {}).items())[:MAX_ATTRIBUTES])
    return dataclasses.replace(
        device, name=device.name.strip()[:MAX_NAME_CHARS] or device.id[:MAX_NAME_CHARS],
        area=(device.area or "").strip()[:MAX_NAME_CHARS],
        aliases=tuple(a.strip()[:MAX_NAME_CHARS] for a in device.aliases if isinstance(a, str) and a.strip())[:10],
        state=str(device.state if device.state is not None else "")[:MAX_STATE_CHARS],
        attributes=attributes)


class Registry:
    """Every visible device, by source. Thread-safe; states change in memory as hubs report them."""

    def __init__(self, hidden: Iterable[str] = ()):
        self._lock = threading.Lock()
        self._by_source: Dict[str, Dict[str, Device]] = {}
        self._hidden = frozenset(str(h) for h in hidden)
        self._listeners: list = []

    @property
    def hidden(self) -> frozenset:
        return self._hidden

    def set_hidden(self, hidden: Iterable[str]) -> None:
        with self._lock:
            self._hidden = frozenset(str(h) for h in hidden)

    def add_listener(self, listener) -> None:
        """``listener(device_id)`` after a device's state changes (MQTT command confirmation)."""
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def replace_source(self, source: str, devices: Iterable[Device]) -> None:
        """Replace every device of one source (a hub's full device list)."""
        kept: Dict[str, Device] = {}
        with self._lock:
            room = MAX_DEVICES - sum(len(d) for s, d in self._by_source.items() if s != source)
            for dev in devices:
                if len(kept) >= max(0, room):
                    break
                if dev.kind in KINDS:
                    kept[dev.id] = _bounded(dev)
            self._by_source[source] = kept
        debug_log(f"smart home devices from {source}: {len(kept)}", "smarthome")

    def upsert(self, dev: Device) -> bool:
        """Add or replace one device (an MQTT discovery message). False when the registry is full."""
        with self._lock:
            devices = self._by_source.setdefault(dev.source, {})
            if dev.id not in devices and sum(len(d) for d in self._by_source.values()) >= MAX_DEVICES:
                return False
            devices[dev.id] = _bounded(dev)
            return True

    def remove(self, device_id: str) -> None:
        with self._lock:
            for devices in self._by_source.values():
                devices.pop(device_id, None)

    def update_state(self, device_id: str, state: Optional[str] = None,
                     attributes: Optional[Mapping[str, Any]] = None) -> bool:
        with self._lock:
            for devices in self._by_source.values():
                dev = devices.get(device_id)
                if dev is None:
                    continue
                merged = dict(dev.attributes)
                merged.update(attributes or {})
                devices[device_id] = _bounded(dataclasses.replace(
                    dev, state=dev.state if state is None else state, attributes=merged))
                listeners = list(self._listeners)
                break
            else:
                return False
        for listener in listeners:
            try:
                listener(device_id)
            except Exception as exc:  # noqa: BLE001 - a listener never stops state updates
                debug_log(f"state listener failed ({type(exc).__name__})", "smarthome")
        return True

    def mark_unavailable(self, source: str) -> None:
        with self._lock:
            devices = self._by_source.get(source, {})
            for key, dev in list(devices.items()):
                devices[key] = dataclasses.replace(dev, state=UNAVAILABLE)

    def clear_source(self, source: str) -> None:
        with self._lock:
            self._by_source.pop(source, None)

    def _visible(self) -> Tuple[Device, ...]:
        with self._lock:
            ha_ids = {d.unique_id for d in self._by_source.get("ha", {}).values() if d.unique_id}
            out = []
            for source in sorted(self._by_source, key=lambda s: (s != "ha", s)):
                for dev in self._by_source[source].values():
                    if dev.id in self._hidden:
                        continue
                    if source != "ha" and dev.unique_id and dev.unique_id in ha_ids:
                        continue  # the Home Assistant copy of the same physical device wins
                    out.append(dev)
            return tuple(out)

    def devices(self, source: Optional[str] = None) -> Tuple[Device, ...]:
        found = self._visible()
        return found if source is None else tuple(d for d in found if d.source == source)

    def raw(self, device_id: str) -> Optional[Device]:
        """A device even when hidden or de-duplicated (connectors only, for state bookkeeping)."""
        with self._lock:
            for devices in self._by_source.values():
                if device_id in devices:
                    return devices[device_id]
        return None

    def get(self, device_id: str) -> Optional[Device]:
        return next((d for d in self._visible() if d.id == device_id), None)

    def count(self, source: Optional[str] = None) -> int:
        return len(self.devices(source))

    def areas(self) -> Tuple[str, ...]:
        seen: Dict[str, str] = {}
        for dev in self._visible():
            if dev.area:
                seen.setdefault(dev.area.casefold(), dev.area)
        return tuple(seen.values())

    def resolve(self, target: str = "", area: str = "", kind: str = "") -> Resolution:
        """Spoken words to devices: exact name, alias or id first, then a unique device whose name holds every
        spoken word. An area with a kind (or alone, with no name) reaches every such device in that area; a
        name that is an area's name, with a kind, does too. Several candidates without an area change nothing."""
        pool = self._visible()
        if kind:
            pool = tuple(d for d in pool if d.kind == kind)
        target = str(target or "").strip()
        area_words = tokens(area)
        if area_words:
            pool = tuple(d for d in pool if tokens(d.area) == area_words)
            if not target:
                return Resolution(devices=pool)
        if not target:
            return Resolution(candidates=pool)
        chosen, candidates = pick(target, [((d.name, d.id, *d.aliases), d) for d in pool])
        if chosen:
            return Resolution(devices=chosen)
        if kind and not area_words and not any(tokens(target) == tokens(d.name) for d in pool):
            in_area = tuple(d for d in pool if d.area and tokens(d.area) == tokens(target))
            if in_area:
                return Resolution(devices=in_area)
        if area_words and candidates:
            return Resolution(devices=candidates)  # several in one named area: the area decides
        return Resolution(candidates=candidates)


@dataclass(frozen=True)
class Command:
    """One change for one device: an action, and for ``set`` the property and its value."""
    action: str
    prop: str = ""
    value: Any = None


@dataclass(frozen=True)
class Outcome:
    """What a hub said about a command. ``confirmed`` only when the hub confirmed it."""
    confirmed: bool
    note: str = ""


# CSS colour names (a technical standard, not a language): the model passes a colour as one of these or as
# #RRGGBB. Home Assistant validates names itself; MQTT devices need the RGB values.
CSS_COLOURS = {
    "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0), "lime": (0, 255, 0),
    "blue": (0, 0, 255), "yellow": (255, 255, 0), "orange": (255, 165, 0), "purple": (128, 0, 128),
    "pink": (255, 192, 203), "cyan": (0, 255, 255), "magenta": (255, 0, 255), "violet": (238, 130, 238),
    "indigo": (75, 0, 130), "gold": (255, 215, 0), "teal": (0, 128, 128), "turquoise": (64, 224, 208),
    "coral": (255, 127, 80), "salmon": (250, 128, 114), "crimson": (220, 20, 60), "lavender": (230, 230, 250),
    "navy": (0, 0, 128), "aqua": (0, 255, 255), "warmwhite": (255, 244, 229), "amber": (255, 191, 0),
}


def parse_colour(value) -> Optional[Tuple[str, Any]]:
    """``("name", css_name)``, ``("rgb", (r, g, b))`` or None. Accepts a CSS name, #RRGGBB or "r,g,b"."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            rgb = tuple(int(c) for c in value)
        except (TypeError, ValueError):
            return None
        return ("rgb", rgb) if all(0 <= c <= 255 for c in rgb) else None
    text = str(value or "").strip().casefold().replace(" ", "")
    if re.fullmatch(r"#?[0-9a-f]{6}", text):
        text = text.lstrip("#")
        return "rgb", tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    if re.fullmatch(r"\d{1,3},\d{1,3},\d{1,3}", text):
        return parse_colour(text.split(","))
    if text in CSS_COLOURS:
        return "name", text
    if re.fullmatch(r"[a-z]{3,20}", text):
        return "name", text  # Home Assistant knows every CSS name; MQTT devices only the table above
    return None


def rgb_of(value) -> Optional[Tuple[int, int, int]]:
    colour = parse_colour(value)
    if colour is None:
        return None
    return colour[1] if colour[0] == "rgb" else CSS_COLOURS.get(colour[1])


# Thermostat settings outside this range always confirm (smarthome.spec.md, safety tiers).
COMFORT_CELSIUS = (10.0, 30.0)
COMFORT_FAHRENHEIT = (50.0, 86.0)


def comfortable(temperature: float, unit: str = "") -> bool:
    low, high = COMFORT_FAHRENHEIT if "F" in str(unit or "").upper() else COMFORT_CELSIUS
    return low <= float(temperature) <= high
