"""The MQTT connector: devices announced in the Home Assistant MQTT discovery format on the user's broker.

Subscribes to ``<prefix>/+/+/config`` and ``<prefix>/+/+/+/config``, builds devices from the discovery
payloads it understands, follows each device's state topic and publishes commands to its command topic,
never retained. Discovery payloads are untrusted: unknown fields are ignored, sizes are capped and topics
are validated before use. ``paho-mqtt`` is imported only when a connection is made and runs on its own
network thread. No tool, reply engine or LLM knowledge. See ``smarthome.spec.md``.
"""
from __future__ import annotations

import dataclasses
import json
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..debug import debug_log
from . import network
from .model import (Capability, Command, Device, Outcome, Registry, UNAVAILABLE, capabilities_for, rgb_of)

SOURCE = "mqtt"
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_TOPIC_CHARS = 256
CONFIRM_TIMEOUT_SEC = 3.0
CONNECT_TIMEOUT_SEC = 5.0
TEST_SETTLE_SEC = 1.0
TEST_MAX_SEC = 5.0
BACKOFF_START_SEC = 2.0
BACKOFF_MAX_SEC = 300.0
COMPONENTS = {"light": "light", "switch": "switch", "fan": "fan", "cover": "cover", "climate": "climate",
              "lock": "lock", "sensor": "sensor", "binary_sensor": "binary_sensor", "button": "button",
              "scene": "scene"}
_TAG = "smarthome"
NOT_CONFIRMED = "The command was sent, but the device does not report its state, so it is not confirmed."
NO_ANSWER = "The command was sent, but the device did not report a new state, so it is not confirmed."

# Home Assistant's discovery abbreviations, for the fields Jarvis reads.
ABBREVIATIONS = {
    "avty_t": "availability_topic", "bri_cmd_t": "brightness_command_topic", "bri_scl": "brightness_scale",
    "bri_stat_t": "brightness_state_topic", "bri_val_tpl": "brightness_value_template",
    "clr_temp_cmd_t": "color_temp_command_topic", "clr_temp_stat_t": "color_temp_state_topic",
    "clr_temp_k": "color_temp_kelvin", "cmd_t": "command_topic", "curr_temp_t": "current_temperature_topic",
    "curr_temp_tpl": "current_temperature_template", "dev": "device", "dev_cla": "device_class",
    "mode_cmd_t": "mode_command_topic", "mode_stat_t": "mode_state_topic", "mode_stat_tpl": "mode_state_template",
    "pct_cmd_t": "percentage_command_topic", "pct_stat_t": "percentage_state_topic",
    "pl_avail": "payload_available", "pl_not_avail": "payload_not_available", "pl_cls": "payload_close",
    "pl_lock": "payload_lock", "pl_off": "payload_off", "pl_on": "payload_on", "pl_open": "payload_open",
    "pl_prs": "payload_press", "pl_stop": "payload_stop", "pl_unlk": "payload_unlock", "pos_t": "position_topic",
    "pos_tpl": "position_template", "set_pos_t": "set_position_topic", "pos_open": "position_open",
    "pos_clsd": "position_closed", "rgb_cmd_t": "rgb_command_topic", "stat_t": "state_topic",
    "stat_on": "state_on", "stat_off": "state_off", "stat_open": "state_open", "stat_clsd": "state_closed",
    "stat_opening": "state_opening", "stat_closing": "state_closing", "stat_locked": "state_locked",
    "stat_unlocked": "state_unlocked", "stat_val_tpl": "state_value_template",
    "sup_clrm": "supported_color_modes", "temp_cmd_t": "temperature_command_topic",
    "temp_stat_t": "temperature_state_topic", "temp_stat_tpl": "temperature_state_template",
    "temp_unit": "temperature_unit", "uniq_id": "unique_id", "unit_of_meas": "unit_of_measurement",
    "val_tpl": "value_template", "obj_id": "object_id", "ret": "retain",
}
_DEVICE_ABBREVIATIONS = {"sa": "suggested_area", "ids": "identifiers", "mf": "manufacturer", "mdl": "model"}
_TEMPLATE = re.compile(r"^\{\{\s*value_json((?:\.[A-Za-z_]\w*|\[['\"][^'\"]{1,40}['\"]\]){1,4})\s*\}\}$")
_DISCOVERY = re.compile(r"^(?P<component>[A-Za-z_]+)/(?:(?P<node>[A-Za-z0-9_-]+)/)?(?P<object>[A-Za-z0-9_-]+)/config$")


class MqttError(Exception):
    """Base for every failure the connector reports. Messages never hold the address or password."""


class MqttAuthError(MqttError):
    pass


class MqttUnreachable(MqttError):
    pass


class MqttNotFound(MqttUnreachable):
    """The broker's hostname does not resolve yet."""


AUTH_MESSAGE = "The MQTT broker refused the user name or password."
UNREACHABLE_MESSAGE = "The MQTT broker did not answer. Check that it is running and the address is right."
NOT_FOUND_MESSAGE = "The MQTT broker's address could not be found on the network yet. Jarvis keeps trying."


def valid_topic(topic: Any) -> bool:
    """A concrete topic: text, 1 to 256 characters, no wildcards or NUL."""
    return (isinstance(topic, str) and 0 < len(topic) <= MAX_TOPIC_CHARS
            and not any(c in topic for c in "+#\x00"))


def _expand(payload: dict) -> dict:
    """Full field names, with ``~`` replaced by the base topic."""
    out = {ABBREVIATIONS.get(k, k): v for k, v in payload.items() if isinstance(k, str)}
    base = out.pop("~", None)
    if isinstance(out.get("device"), dict):
        out["device"] = {_DEVICE_ABBREVIATIONS.get(k, k): v for k, v in out["device"].items()}
    if isinstance(base, str):
        for key, value in list(out.items()):
            if key.endswith("_topic") and isinstance(value, str):
                if value.startswith("~"):
                    out[key] = base + value[1:]
                elif value.endswith("~"):
                    out[key] = value[:-1] + base
    if isinstance(out.get("availability"), list) and out["availability"] and not out.get("availability_topic"):
        first = out["availability"][0]
        if isinstance(first, dict):
            topic = first.get("topic") or first.get("t")
            if isinstance(topic, str) and base and topic.startswith("~"):
                topic = base + topic[1:]
            out["availability_topic"] = topic
    return out


def _value(raw: bytes, template: Any) -> Optional[Any]:
    """The value a state payload carries: the raw text, or one field for a ``{{ value_json.x }}`` template."""
    if len(raw) > MAX_PAYLOAD_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not template:
        return text.strip()
    match = _TEMPLATE.match(str(template).strip())
    if not match:
        return None  # a template Jarvis cannot evaluate: the value stays unknown rather than wrong
    try:
        value: Any = json.loads(text)
    except ValueError:
        return None
    for part in re.findall(r"\.([A-Za-z_]\w*)|\[['\"]([^'\"]+)['\"]\]", match.group(1)):
        key = part[0] or part[1]
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


@dataclass
class _Spec:
    """What Jarvis keeps of one discovery payload: topics and payloads only."""
    component: str
    config: Dict[str, Any]
    json_schema: bool = False
    state_topics: List[Tuple[str, str]] = field(default_factory=list)   # (topic, role: state, brightness, ...)

    def get(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value


def _text_or(value: Any, default: str) -> str:
    return value if isinstance(value, str) and len(value) <= 256 else default


def parse_discovery(component: str, object_id: str, payload: dict) -> Optional[Tuple[Device, _Spec]]:
    """A device and its topics from one discovery payload, or None when it is unusable or unsafe."""
    kind = COMPONENTS.get(component)
    if kind is None or not isinstance(payload, dict):
        return None
    config = _expand(payload)
    json_schema = config.get("schema") == "json"
    topics = {k: v for k, v in config.items() if k.endswith("_topic") and v is not None}
    if any(not valid_topic(v) for v in topics.values()):
        return None
    if kind not in ("sensor", "binary_sensor") and not topics.get("command_topic"):
        if not (kind == "climate" and (topics.get("temperature_command_topic") or topics.get("mode_command_topic"))):
            return None
    unique_id = config.get("unique_id")
    unique_id = unique_id if isinstance(unique_id, str) and 0 < len(unique_id) <= 200 else ""
    key = unique_id or f"{component}_{object_id}"
    if not re.fullmatch(r"[\w.\-:]{1,200}", key):
        return None
    dev = config.get("device") if isinstance(config.get("device"), dict) else {}
    entity_name = config.get("name") if isinstance(config.get("name"), str) else ""
    device_name = dev.get("name") if isinstance(dev.get("name"), str) else ""
    name = f"{device_name} {entity_name}".strip() if device_name and entity_name else entity_name or device_name
    area = dev.get("suggested_area") if isinstance(dev.get("suggested_area"), str) else ""
    spec = _Spec(component, config, json_schema)
    for role in ("state_topic", "brightness_state_topic", "percentage_state_topic", "position_topic",
                 "temperature_state_topic", "current_temperature_topic", "mode_state_topic",
                 "color_temp_state_topic", "availability_topic"):
        if topics.get(role):
            spec.state_topics.append((topics[role], role))
    device = Device(
        id=f"mqtt:{key}", name=name or key, kind=kind, source=SOURCE, area=area, state="",
        capabilities=capabilities_for(kind, _capabilities(kind, config, json_schema)),
        attributes=({"unit_of_measurement": config["unit_of_measurement"]}
                    if isinstance(config.get("unit_of_measurement"), str) else {})
        | ({"temperature_unit": config["temperature_unit"]} if isinstance(config.get("temperature_unit"), str)
           else {}),
        device_class=_text_or(config.get("device_class"), ""), unique_id=unique_id)
    return device, spec


def _capabilities(kind: str, config: dict, json_schema: bool) -> Optional[set]:
    if kind == "light":
        caps = {Capability.ON_OFF}
        modes = config.get("supported_color_modes") if isinstance(config.get("supported_color_modes"), list) else []
        if json_schema:
            if config.get("brightness") is True or any(m != "onoff" for m in modes):
                caps.add(Capability.BRIGHTNESS)
            if set(modes) & {"rgb", "rgbw", "rgbww", "hs", "xy"}:
                caps.add(Capability.COLOUR)
            if "color_temp" in modes:
                caps.add(Capability.COLOUR_TEMPERATURE)
        else:
            if config.get("brightness_command_topic"):
                caps.add(Capability.BRIGHTNESS)
            if config.get("rgb_command_topic"):
                caps.add(Capability.COLOUR)
            if config.get("color_temp_command_topic"):
                caps.add(Capability.COLOUR_TEMPERATURE)
        return caps
    if kind == "fan":
        return {Capability.ON_OFF} | ({Capability.SPEED} if config.get("percentage_command_topic") else set())
    if kind == "cover":
        caps = {Capability.OPEN_CLOSE} if config.get("command_topic") else set()
        if config.get("command_topic") and config.get("payload_stop", "STOP") is not None:
            caps.add(Capability.STOP)
        if config.get("set_position_topic"):
            caps.add(Capability.POSITION)
        return caps
    if kind == "climate":
        caps = {Capability.TARGET_TEMPERATURE} if config.get("temperature_command_topic") else set()
        return caps | ({Capability.MODE} if config.get("mode_command_topic") else set())
    return None


def _on_off(spec: _Spec, value: Any) -> Optional[str]:
    on = _text_or(spec.get("state_on", spec.get("payload_on", "ON")), "ON")
    off = _text_or(spec.get("state_off", spec.get("payload_off", "OFF")), "OFF")
    text = str(value)
    return "on" if text == on else "off" if text == off else None


def state_update(spec: _Spec, role: str, raw: bytes) -> Tuple[Optional[str], Dict[str, Any]]:
    """The new state and attributes a message on one of the device's topics means."""
    config = spec.config
    if role == "availability_topic":
        value = _value(raw, None)
        offline = _text_or(spec.get("payload_not_available", "offline"), "offline")
        return (UNAVAILABLE if value == offline else None), {}
    if spec.json_schema and role == "state_topic":
        data = _value(raw, None)
        try:
            data = json.loads(data) if data is not None else None
        except ValueError:
            return None, {}
        if not isinstance(data, dict):
            return None, {}
        attributes = {}
        if isinstance(data.get("brightness"), (int, float)):
            scale = spec.get("brightness_scale", 255) or 255
            attributes["brightness_percent"] = round(float(data["brightness"]) * 100 / float(scale))
        state = _on_off(spec, data.get("state")) if "state" in data else None
        return state, attributes
    template = {"state_topic": config.get("state_value_template") or config.get("value_template"),
                "brightness_state_topic": config.get("brightness_value_template"),
                "position_topic": config.get("position_template"),
                "temperature_state_topic": config.get("temperature_state_template"),
                "current_temperature_topic": config.get("current_temperature_template"),
                "mode_state_topic": config.get("mode_state_template")}.get(role)
    value = _value(raw, template)
    if value is None:
        return None, {}
    kind = COMPONENTS[spec.component]
    if role == "state_topic":
        if kind in ("light", "switch", "fan", "binary_sensor"):
            return _on_off(spec, value), {}
        if kind == "lock":
            locked = _text_or(spec.get("state_locked", "LOCKED"), "LOCKED")
            unlocked = _text_or(spec.get("state_unlocked", "UNLOCKED"), "UNLOCKED")
            return ("locked" if str(value) == locked else "unlocked" if str(value) == unlocked else None), {}
        if kind == "cover":
            names = {_text_or(spec.get("state_open", "open"), "open"): "open",
                     _text_or(spec.get("state_closed", "closed"), "closed"): "closed",
                     _text_or(spec.get("state_opening", "opening"), "opening"): "opening",
                     _text_or(spec.get("state_closing", "closing"), "closing"): "closing"}
            return names.get(str(value)), {}
        return str(value)[:100], {}
    number = _number(value)
    if role == "brightness_state_topic" and number is not None:
        scale = spec.get("brightness_scale", 255) or 255
        return None, {"brightness_percent": round(number * 100 / float(scale))}
    if role == "percentage_state_topic" and number is not None:
        return None, {"percentage": number}
    if role == "position_topic" and number is not None:
        return None, {"current_position": number}
    if role == "temperature_state_topic" and number is not None:
        return None, {"temperature": number}
    if role == "current_temperature_topic" and number is not None:
        return None, {"current_temperature": number}
    if role == "mode_state_topic":
        return str(value)[:30], {}
    return None, {}


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) < 1e9 else None


def command_messages(device: Device, spec: _Spec, command: Command) -> List[Tuple[str, str]]:
    """``(topic, payload)`` pairs for a command, or ``ValueError`` when the device cannot do it."""
    config, action, kind = spec.config, command.action, device.kind
    topic = config.get("command_topic")

    def payload(key: str, default: str) -> str:
        return _text_or(spec.get(key, default), default)

    if kind == "light" and spec.json_schema:
        body: Dict[str, Any]
        if action in ("turn_on", "turn_off", "toggle"):
            on = action == "turn_on" or (action == "toggle" and device.state != "on")
            body = {"state": "ON" if on else "OFF"}
        elif action == "set" and command.prop == "brightness":
            scale = spec.get("brightness_scale", 255) or 255
            body = {"state": "ON", "brightness": round(int(command.value) * float(scale) / 100)}
        elif action == "set" and command.prop == "colour":
            rgb = rgb_of(command.value)
            if rgb is None:
                raise ValueError("colour")
            body = {"state": "ON", "color": {"r": rgb[0], "g": rgb[1], "b": rgb[2]}}
        elif action == "set" and command.prop == "colour_temperature":
            body = {"state": "ON", "color_temp": round(1_000_000 / max(1000, int(command.value)))}
        else:
            raise ValueError(action)
        return [(topic, json.dumps(body))]
    if kind in ("light", "switch", "fan") and action in ("turn_on", "turn_off", "toggle"):
        on = action == "turn_on" or (action == "toggle" and device.state != "on")
        return [(topic, payload("payload_on", "ON") if on else payload("payload_off", "OFF"))]
    if action == "set":
        prop, value = command.prop, command.value
        if kind == "light" and prop == "brightness" and config.get("brightness_command_topic"):
            scale = spec.get("brightness_scale", 255) or 255
            return [(config["brightness_command_topic"], str(round(int(value) * float(scale) / 100)))]
        if kind == "light" and prop == "colour" and config.get("rgb_command_topic"):
            rgb = rgb_of(value)
            if rgb is None:
                raise ValueError(prop)
            return [(config["rgb_command_topic"], ",".join(str(c) for c in rgb))]
        if kind == "light" and prop == "colour_temperature" and config.get("color_temp_command_topic"):
            kelvin = int(value)
            return [(config["color_temp_command_topic"],
                     str(kelvin if config.get("color_temp_kelvin") is True else round(1_000_000 / max(1000, kelvin))))]
        if kind == "fan" and prop == "speed" and config.get("percentage_command_topic"):
            return [(config["percentage_command_topic"], str(int(value)))]
        if kind == "cover" and prop == "position" and config.get("set_position_topic"):
            return [(config["set_position_topic"], str(int(value)))]
        if kind == "climate" and prop == "temperature" and config.get("temperature_command_topic"):
            return [(config["temperature_command_topic"], str(value))]
        if kind == "climate" and prop == "mode" and config.get("mode_command_topic"):
            return [(config["mode_command_topic"], str(value))]
        raise ValueError(prop)
    simple = {
        ("cover", "open"): ("payload_open", "OPEN"), ("cover", "close"): ("payload_close", "CLOSE"),
        ("cover", "stop"): ("payload_stop", "STOP"), ("lock", "lock"): ("payload_lock", "LOCK"),
        ("lock", "unlock"): ("payload_unlock", "UNLOCK"), ("button", "activate"): ("payload_press", "PRESS"),
        ("scene", "activate"): ("payload_on", "ON"),
    }
    if (kind, action) in simple and topic:
        key, default = simple[(kind, action)]
        return [(topic, payload(key, default))]
    raise ValueError(action)


# -- the connection ---------------------------------------------------------------------------------


def _paho():
    import paho.mqtt.client as client  # paho-mqtt, imported only when a connection is made
    return client


def _make_client(endpoint: network.Endpoint, username: str, password: str, tls: bool, verify_tls: bool,
                 client_id: str):
    paho = _paho()

    class PinnedClient(paho.Client):
        """Connects to the address validated at start, whatever DNS says later, and never through a proxy."""

        def _create_socket_connection(self):
            network.check_connect(endpoint.ip)
            return socket.create_connection((endpoint.ip, endpoint.port), timeout=CONNECT_TIMEOUT_SEC)

    client = PinnedClient(callback_api_version=paho.CallbackAPIVersion.VERSION2, client_id=client_id,
                          protocol=paho.MQTTv311)
    if username:
        client.username_pw_set(username, password or None)
    if tls:
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(context)
    client.reconnect_delay_set(min_delay=2, max_delay=300)
    return client


def _endpoint(host: str, port: int, *, resolve: bool = True) -> network.Endpoint:
    try:
        found = network.validate_endpoint(host, default_port=port, schemes=("mqtt",), resolve=resolve)
    except network.SmartHomeNotFound:
        raise MqttNotFound(NOT_FOUND_MESSAGE) from None
    except network.SmartHomeAddressError as exc:
        raise MqttError(f"The MQTT broker address was refused: {exc}") from None
    return network.Endpoint(found.scheme, found.host, found.ip, int(port or found.port))


class MqttConnector:
    """A long-lived broker connection that keeps ``registry`` in step with discovered devices."""

    def __init__(self, registry: Registry, host: str, port: int = 1883, *, username: str = "",
                 password: str = "", tls: bool = False, verify_tls: bool = True,
                 discovery_prefix: str = "homeassistant", follow_states: bool = True):
        self._registry = registry
        # A connection test only counts announced devices: it never subscribes to state topics.
        self._follow_states = follow_states
        self._host, self._port = host, int(port or 1883)
        self._username, self._password = username, password
        self._tls, self._verify_tls = tls, verify_tls
        prefix = (discovery_prefix or "homeassistant").strip().strip("/")
        self._prefix = prefix if valid_topic(prefix) else "homeassistant"
        self._client = None
        self._lock = threading.Lock()
        self._specs: Dict[str, _Spec] = {}
        self._routes: Dict[str, List[Tuple[str, str]]] = {}   # topic -> [(device id, role)]
        self._discovered: Dict[str, str] = {}     # discovery topic key -> device id
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        # starting | connected | auth | unreachable | not_found | refused | missing_library | stopped
        self.status = "starting"

    @property
    def discovery_topics(self) -> Tuple[str, str]:
        return f"{self._prefix}/+/+/config", f"{self._prefix}/+/+/+/config"

    def start(self) -> None:
        """Check the library and the address syntax, then resolve and connect on a background thread that
        keeps retrying a name that does not resolve yet. Raises ``ImportError`` without paho-mqtt and
        ``MqttError`` for an address that can never be allowed."""
        if self._thread is not None:
            return
        try:
            _paho()
        except ImportError:
            self.status = "missing_library"
            debug_log("MQTT connector not started: paho-mqtt missing", _TAG)
            raise
        try:
            _endpoint(self._host, self._port, resolve=False)
        except MqttError:
            self.status = "refused"
            debug_log("MQTT connector not started: address refused", _TAG)
            raise
        debug_log("MQTT connector starting", _TAG)
        self._thread = threading.Thread(target=self._connect_loop, name="smarthome-mqtt", daemon=True)
        self._thread.start()

    def _connect_loop(self) -> None:
        delay = BACKOFF_START_SEC
        while not self._stop.is_set():
            try:
                endpoint = _endpoint(self._host, self._port)
            except MqttNotFound:
                self.status = "not_found"
                debug_log("MQTT broker address not found yet", _TAG)
                self._stop.wait(delay)
                delay = min(delay * 2, BACKOFF_MAX_SEC)
                continue
            except MqttError:
                self.status = "refused"
                debug_log("MQTT connector stopped: address refused", _TAG)
                return
            client = _make_client(endpoint, self._username, self._password, self._tls, self._verify_tls,
                                  client_id=f"jarvis-{int(time.time() * 1000) % 10_000_000}")
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            if self._stop.is_set():
                return
            self._client = client
            # paho reconnects by itself from here, always to the address resolved now.
            client.connect_async(endpoint.host, endpoint.port, keepalive=60)
            client.loop_start()
            return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception as exc:  # noqa: BLE001 - stopping never raises
                debug_log(f"MQTT stop: {type(exc).__name__}", _TAG)
        self.connected = False
        self.status = "stopped"
        debug_log("MQTT connector stopped", _TAG)

    # -- paho callbacks (network thread) -------------------------------------------------------

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self.status = "auth" if getattr(reason_code, "value", 0) in (4, 5, 134, 135) else "refused"
            debug_log(f"MQTT connection failed: {self.status}", _TAG)
            return
        self.connected = True
        self.status = "connected"
        for topic in self.discovery_topics:
            client.subscribe(topic, qos=0)
        with self._lock:
            topics = [t for t in self._routes] if self._follow_states else []
        for topic in topics:
            client.subscribe(topic, qos=0)
        debug_log("MQTT connected", _TAG)

    def _on_disconnect(self, _client, _userdata, _flags, _reason_code=None, _properties=None) -> None:
        if self.connected:
            self._registry.mark_unavailable(SOURCE)
        self.connected = False
        if self.status == "connected":
            self.status = "unreachable"
        debug_log("MQTT disconnected", _TAG)

    def _on_message(self, client, _userdata, message) -> None:
        try:
            topic = message.topic
            if topic.startswith(self._prefix + "/") and topic.endswith("/config"):
                self._discovery(client, topic[len(self._prefix) + 1:], message.payload)
            else:
                self._state(topic, message.payload)
        except Exception as exc:  # noqa: BLE001 - one bad message never stops the connector
            debug_log(f"MQTT message ignored ({type(exc).__name__})", _TAG)

    def _discovery(self, client, rest: str, raw: bytes) -> None:
        match = _DISCOVERY.match(rest)
        if match is None or len(raw) > MAX_PAYLOAD_BYTES:
            return
        component = match.group("component")
        # The node id is part of the topic's identity: two nodes may both announce "relay".
        object_id = "_".join(part for part in (match.group("node"), match.group("object")) if part)
        key = f"{component}/{object_id}"
        if not raw:
            self._forget(f"mqtt:{component}_{object_id}", key)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        parsed = parse_discovery(component, object_id, payload) if isinstance(payload, dict) else None
        if parsed is None:
            return
        device, spec = parsed
        old = self._registry.raw(device.id)
        if old is not None:
            device = dataclasses.replace(device, state=old.state, attributes={**old.attributes, **device.attributes})
        if not self._registry.upsert(device):
            return
        new_topics = []
        with self._lock:
            self._specs[device.id] = spec
            self._discovered[key] = device.id
            for routes in self._routes.values():
                routes[:] = [r for r in routes if r[0] != device.id]
            for topic, role in spec.state_topics:
                if topic not in self._routes or not self._routes[topic]:
                    new_topics.append(topic)
                self._routes.setdefault(topic, []).append((device.id, role))
        for topic in new_topics if self._follow_states else ():
            client.subscribe(topic, qos=0)

    def _forget(self, fallback_id: str, key: str) -> None:
        with self._lock:
            device_id = self._discovered.pop(key, fallback_id)
            self._specs.pop(device_id, None)
            for routes in self._routes.values():
                routes[:] = [r for r in routes if r[0] != device_id]
        self._registry.remove(device_id)

    def _state(self, topic: str, raw: bytes) -> None:
        with self._lock:
            routes = list(self._routes.get(topic, ()))
            specs = {device_id: self._specs.get(device_id) for device_id, _role in routes}
        for device_id, role in routes:
            spec = specs.get(device_id)
            if spec is None:
                continue
            state, attributes = state_update(spec, role, raw)
            if state is not None or attributes:
                self._registry.update_state(device_id, state, attributes)

    # -- commands (tool threads) -----------------------------------------------------------------

    def send(self, device: Device, command: Command) -> Outcome:
        client = self._client
        if client is None or not self.connected:
            raise MqttUnreachable("Jarvis is not connected to the MQTT broker right now.")
        with self._lock:
            spec = self._specs.get(device.id)
        if spec is None:
            raise MqttError("That device is no longer announced on the MQTT broker.")
        try:
            messages = command_messages(device, spec, command)
        except (ValueError, TypeError):
            raise MqttError("That device cannot do that.") from None
        if any(not valid_topic(topic) for topic, _payload in messages):
            raise MqttError("That device's command topic is not valid.")
        changed = threading.Event()

        def listener(device_id: str) -> None:
            if device_id == device.id:
                changed.set()

        reports = any(role != "availability_topic" for _topic, role in spec.state_topics)
        if reports:
            self._registry.add_listener(listener)
        try:
            for topic, payload in messages:
                info = client.publish(topic, payload, qos=0, retain=False)
                if getattr(info, "rc", 0) != 0:
                    raise MqttUnreachable("The MQTT broker did not take the command.")
            debug_log(f"MQTT command sent ({device.kind} {command.action})", _TAG)
            if not reports:
                return Outcome(confirmed=False, note=NOT_CONFIRMED)
            if changed.wait(CONFIRM_TIMEOUT_SEC):
                return Outcome(confirmed=True)
            return Outcome(confirmed=False, note=NO_ANSWER)
        finally:
            if reports:
                self._registry.remove_listener(listener)


def test_connection(host: str, port: int = 1883, *, username: str = "", password: str = "", tls: bool = False,
                    verify_tls: bool = True, discovery_prefix: str = "homeassistant") -> int:
    """Connect once, collect the announced devices for a moment and disconnect: how many Jarvis can use."""
    endpoint = _endpoint(host, port)
    registry = Registry()
    connector = MqttConnector(registry, host, port, username=username, password=password, tls=tls,
                              verify_tls=verify_tls, discovery_prefix=discovery_prefix, follow_states=False)
    client = _make_client(endpoint, username, password, tls, verify_tls, client_id="jarvis-test")
    answered = threading.Event()
    outcome: Dict[str, Any] = {}

    def on_connect(c, userdata, flags, reason_code, properties=None):
        outcome["failure"] = getattr(reason_code, "is_failure", False)
        outcome["code"] = getattr(reason_code, "value", 0)
        if not outcome["failure"]:
            for topic in connector.discovery_topics:
                c.subscribe(topic, qos=0)
        answered.set()

    def on_message(c, userdata, message):
        outcome["last"] = time.monotonic()
        connector._on_message(c, userdata, message)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(endpoint.host, endpoint.port, keepalive=30)
    except (OSError, ssl.SSLError):
        raise MqttUnreachable(UNREACHABLE_MESSAGE) from None
    client.loop_start()
    try:
        if not answered.wait(CONNECT_TIMEOUT_SEC):
            raise MqttUnreachable(UNREACHABLE_MESSAGE)
        if outcome.get("failure"):
            if outcome.get("code") in (4, 5, 134, 135):
                raise MqttAuthError(AUTH_MESSAGE)
            raise MqttError("The MQTT broker refused the connection.")
        outcome["last"] = time.monotonic()
        give_up = time.monotonic() + TEST_MAX_SEC
        while time.monotonic() - outcome["last"] < TEST_SETTLE_SEC and time.monotonic() < give_up:
            time.sleep(0.05)
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:  # noqa: BLE001 - closing a test connection
            pass
    count = registry.count()
    debug_log(f"MQTT test: {count} devices", _TAG)
    return count
