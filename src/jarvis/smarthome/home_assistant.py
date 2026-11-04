"""The Home Assistant connector: one WebSocket connection to the user's own Home Assistant.

After ``auth`` it reads the states, the entity, device and area registries and the exposed-entity list,
keeps only entities exposed to voice assistants, then follows ``state_changed`` events. Commands are
``call_service`` messages answered within ``COMMAND_TIMEOUT_SEC``. One daemon thread reads the socket and
never runs tools. ``websocket-client`` is imported only when a connection is made. No tool, reply engine
or LLM knowledge. See ``smarthome.spec.md``.
"""
from __future__ import annotations

import itertools
import json
import socket
import ssl
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..debug import debug_log
from . import network
from .model import (Capability, Command, Device, Outcome, Registry, UNAVAILABLE, capabilities_for, parse_colour)

COMMAND_TIMEOUT_SEC = 5.0
LOAD_TIMEOUT_SEC = 10.0
BACKOFF_START_SEC = 2.0
BACKOFF_MAX_SEC = 300.0
PING_AFTER_SEC = 60.0
MAX_MISSED_PINGS = 2
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_ERROR_CHARS = 200
SOURCE = "ha"
_TAG = "smarthome"

# Home Assistant domains Jarvis uses, and the device kind each becomes. Media players are not included.
DOMAIN_KINDS = {
    "light": "light", "switch": "switch", "fan": "fan", "cover": "cover", "climate": "climate",
    "lock": "lock", "alarm_control_panel": "alarm", "sensor": "sensor", "binary_sensor": "binary_sensor",
    "scene": "scene", "script": "script", "button": "button",
}
KIND_DOMAINS = {kind: domain for domain, kind in DOMAIN_KINDS.items()}
_KEPT_ATTRIBUTES = ("brightness", "current_position", "temperature", "current_temperature", "target_temp_high",
                    "target_temp_low", "hvac_modes", "hvac_action", "percentage", "color_temp_kelvin",
                    "unit_of_measurement", "min_temp", "max_temp", "battery_level")


class HomeAssistantError(Exception):
    """Base for every failure the connector reports. Messages never hold the address or token."""


class HomeAssistantAuthError(HomeAssistantError):
    pass


class HomeAssistantUnreachable(HomeAssistantError):
    pass


class HomeAssistantNotFound(HomeAssistantUnreachable):
    """The address is a hostname that does not resolve yet."""


class HomeAssistantCommandRejected(HomeAssistantError):
    """``summary`` is Jarvis's own words; ``detail`` is Home Assistant's reason, untrusted text."""

    def __init__(self, summary: str, detail: str = ""):
        super().__init__(f"{summary} {detail}".strip() if detail else summary)
        self.summary = summary
        self.detail = detail
        self.code = ""


AUTH_MESSAGE = "Home Assistant refused the access token, create a new one."
UNREACHABLE_MESSAGE = "Home Assistant did not answer. Check that it is running and the address is right."
_REDIRECT_MESSAGE = "Home Assistant redirected the connection elsewhere; Jarvis does not follow redirects."
ADMIN_MESSAGE = ("Home Assistant only tells an administrator which devices are exposed to voice assistants. Create "
                 "the access token while signed in as an administrator.")
NOT_FOUND_MESSAGE = ("Home Assistant's address could not be found on the network yet (it may still be starting). "
                     "Jarvis keeps trying.")
NOT_CONNECTED_MESSAGE = "Jarvis is not connected to Home Assistant right now."


# -- building devices -------------------------------------------------------------------------------


def _light_capabilities(attributes: dict) -> Optional[set]:
    modes = attributes.get("supported_color_modes")
    if not isinstance(modes, list):
        return None
    caps = {Capability.ON_OFF}
    if any(m != "onoff" for m in modes):
        caps.add(Capability.BRIGHTNESS)
    if "color_temp" in modes:
        caps.add(Capability.COLOUR_TEMPERATURE)
    if set(modes) & {"hs", "rgb", "rgbw", "rgbww", "xy"}:
        caps.add(Capability.COLOUR)
    return caps


def _features(attributes: dict) -> Optional[int]:
    value = attributes.get("supported_features")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _narrowed(kind: str, attributes: dict) -> Optional[set]:
    """What the entity says it supports, or None to keep the kind's full set."""
    if kind == "light":
        return _light_capabilities(attributes)
    features = _features(attributes)
    if features is None:
        return None
    if kind == "fan":
        return {Capability.ON_OFF} | ({Capability.SPEED} if features & 1 else set())
    if kind == "cover":
        caps = set()
        if features & 3:
            caps.add(Capability.OPEN_CLOSE)
        if features & 4:
            caps.add(Capability.POSITION)
        if features & 8:
            caps.add(Capability.STOP)
        return caps
    if kind == "climate":
        caps = {Capability.TARGET_TEMPERATURE} if features & 1 else set()
        return caps | ({Capability.MODE} if attributes.get("hvac_modes") else set())
    return None


def build_devices(states: Iterable[dict], entities: Iterable[dict], devices: Iterable[dict],
                  areas: Iterable[dict], exposed: Dict[str, Any], extended: Dict[str, Any],
                  temperature_unit: str = "") -> List[Device]:
    """Devices from Home Assistant's own data: only exposed entities of supported domains."""
    entity_by_id = {e.get("entity_id"): e for e in entities if isinstance(e, dict)}
    device_by_id = {d.get("id"): d for d in devices if isinstance(d, dict)}
    area_names = {a.get("area_id"): a.get("name") for a in areas if isinstance(a, dict)}
    out = []
    for state in states:
        if not isinstance(state, dict):
            continue
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or "." not in entity_id:
            continue
        kind = DOMAIN_KINDS.get(entity_id.split(".", 1)[0])
        settings = exposed.get(entity_id)
        if kind is None or not isinstance(settings, dict) or settings.get("conversation") is not True:
            continue
        reg = entity_by_id.get(entity_id) or {}
        if reg.get("disabled_by"):
            continue
        attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
        name = reg.get("name") or attributes.get("friendly_name") or entity_id
        area_id = reg.get("area_id") or (device_by_id.get(reg.get("device_id")) or {}).get("area_id")
        aliases = (extended.get(entity_id) or {}).get("aliases") or ()
        kept = {key: attributes[key] for key in _KEPT_ATTRIBUTES if key in attributes}
        if temperature_unit and kind == "climate":
            kept["temperature_unit"] = temperature_unit
        out.append(Device(
            id=f"ha:{entity_id}", name=str(name), kind=kind, source=SOURCE,
            area=str(area_names.get(area_id) or ""), aliases=tuple(str(a) for a in aliases if isinstance(a, str)),
            state=str(state.get("state") or ""), capabilities=capabilities_for(kind, _narrowed(kind, attributes)),
            attributes=kept, device_class=str(attributes.get("device_class") or ""),
            unique_id=str(reg.get("unique_id") or "") if reg.get("platform") == "mqtt" else ""))
    return out


def service_call(device: Device, command: Command) -> Tuple[str, str, dict]:
    """The ``(domain, service, service_data)`` for a command, or ``ValueError`` when the device cannot do it."""
    kind, action = device.kind, command.action
    domain = KIND_DOMAINS.get(kind)
    simple = {
        ("light", "turn_on"): "turn_on", ("light", "turn_off"): "turn_off", ("light", "toggle"): "toggle",
        ("switch", "turn_on"): "turn_on", ("switch", "turn_off"): "turn_off", ("switch", "toggle"): "toggle",
        ("fan", "turn_on"): "turn_on", ("fan", "turn_off"): "turn_off", ("fan", "toggle"): "toggle",
        ("cover", "open"): "open_cover", ("cover", "close"): "close_cover", ("cover", "stop"): "stop_cover",
        ("lock", "lock"): "lock", ("lock", "unlock"): "unlock",
        ("alarm", "arm"): "alarm_arm_away", ("alarm", "disarm"): "alarm_disarm",
        ("scene", "activate"): "turn_on", ("script", "activate"): "turn_on", ("button", "activate"): "press",
    }
    if (kind, action) in simple:
        return domain, simple[(kind, action)], {}
    if action != "set":
        raise ValueError(action)
    prop, value = command.prop, command.value
    if kind == "light" and prop == "brightness":
        return domain, "turn_on", {"brightness_pct": int(value)}
    if kind == "light" and prop == "colour":
        colour = parse_colour(value)
        if colour is None:
            raise ValueError(prop)
        return domain, "turn_on", ({"color_name": colour[1]} if colour[0] == "name" else {"rgb_color": list(colour[1])})
    if kind == "light" and prop == "colour_temperature":
        return domain, "turn_on", {"color_temp_kelvin": int(value)}
    if kind == "fan" and prop == "speed":
        return domain, "set_percentage", {"percentage": int(value)}
    if kind == "cover" and prop == "position":
        return domain, "set_cover_position", {"position": int(value)}
    if kind == "climate" and prop == "temperature":
        return domain, "set_temperature", {"temperature": value}
    if kind == "climate" and prop == "mode":
        return domain, "set_hvac_mode", {"hvac_mode": str(value)}
    raise ValueError(prop)


# -- the WebSocket session --------------------------------------------------------------------------


def _websocket():
    import websocket  # websocket-client, imported only when a connection is made
    return websocket


def _endpoint(url: str) -> network.Endpoint:
    try:
        return network.validate_endpoint(url, default_port=8123)
    except network.SmartHomeNotFound:
        raise HomeAssistantNotFound(NOT_FOUND_MESSAGE) from None
    except network.SmartHomeAddressError as exc:
        raise HomeAssistantError(f"The Home Assistant address was refused: {exc}") from None


def _socket(endpoint: network.Endpoint, verify_tls: bool, timeout: float):
    """A socket to the resolved address (TLS named after the host the user typed), never through a proxy."""
    network.check_connect(endpoint.ip)
    try:
        sock = socket.create_connection((endpoint.ip, endpoint.port), timeout=timeout)
    except OSError:
        raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE) from None
    if endpoint.scheme != "https":
        return sock
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        return context.wrap_socket(sock, server_hostname=endpoint.host)
    except (ssl.SSLError, OSError):
        sock.close()
        raise HomeAssistantUnreachable(
            "Home Assistant's certificate was not accepted. For a self-signed certificate, turn off certificate "
            "checking in Settings > Smart Home.") from None


def _open(url: str, token: str, verify_tls: bool, timeout: float):
    """An authenticated WebSocket, or a ``HomeAssistantError``. Never follows a redirect or a proxy."""
    endpoint = _endpoint(url)
    websocket = _websocket()
    sock = _socket(endpoint, verify_tls, timeout)
    scheme = "wss" if endpoint.scheme == "https" else "ws"
    ws = websocket.WebSocket(skip_utf8_validation=False)
    try:
        ws.connect(f"{scheme}://{endpoint.host_for_url}:{endpoint.port}/api/websocket", socket=sock,
                   timeout=timeout, host=f"{endpoint.host_for_url}:{endpoint.port}", suppress_origin=True,
                   redirect_limit=0)
    except websocket.WebSocketBadStatusException:
        _close(ws, sock)
        raise HomeAssistantError("Home Assistant did not accept the connection at that address.") from None
    except websocket.WebSocketException as exc:
        _close(ws, sock)
        if "redirect" in str(exc).casefold():
            raise HomeAssistantError(_REDIRECT_MESSAGE) from None
        raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE) from None
    except OSError:
        _close(ws, sock)
        raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE) from None
    if getattr(ws, "status", 101) != 101:  # a redirect is answered, never followed
        _close(ws, sock)
        raise HomeAssistantError(_REDIRECT_MESSAGE)
    try:
        first = _read(ws)
        if first.get("type") != "auth_required":
            raise HomeAssistantError("That address is not a Home Assistant server.")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        answer = _read(ws)
    except HomeAssistantError:
        _close(ws, sock)
        raise
    if answer.get("type") == "auth_invalid":
        _close(ws, sock)
        raise HomeAssistantAuthError(AUTH_MESSAGE)
    if answer.get("type") != "auth_ok":
        _close(ws, sock)
        raise HomeAssistantError("That address is not a Home Assistant server.")
    return ws


def _read(ws) -> dict:
    websocket = _websocket()
    try:
        raw = ws.recv()
    except (OSError, websocket.WebSocketException):
        raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE) from None
    return _parse(raw)


def _parse(raw) -> dict:
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE)
    try:
        message = json.loads(raw)
    except ValueError:
        raise HomeAssistantError("Home Assistant sent something Jarvis cannot read.") from None
    if not isinstance(message, dict):
        raise HomeAssistantError("Home Assistant sent something Jarvis cannot read.")
    return message


def _close(ws, sock=None) -> None:
    for closer in (getattr(ws, "close", None), getattr(sock, "close", None)):
        try:
            if closer is not None:
                closer()
        except Exception:  # noqa: BLE001 - closing a broken socket
            pass


def _rejected(error: Any) -> HomeAssistantCommandRejected:
    message = error.get("message") if isinstance(error, dict) else ""
    text = " ".join(str(message or "no reason given").split())[:MAX_ERROR_CHARS]
    rejected = HomeAssistantCommandRejected("Home Assistant rejected the command.", detail=text)
    rejected.code = str(error.get("code") or "") if isinstance(error, dict) else ""
    return rejected


def _sync_requester(ws, ids, deadline: float) -> Callable[..., Any]:
    """Requests answered in order on a socket nobody else reads, all within ``deadline``."""

    def request(kind: str, **extra) -> Any:
        mid = next(ids)
        ws.send(json.dumps({"id": mid, "type": kind, **extra}))
        while True:
            if time.monotonic() > deadline:
                raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE)
            ws.settimeout(max(0.05, min(1.0, deadline - time.monotonic())))
            try:
                message = _read(ws)
            except HomeAssistantUnreachable:
                if time.monotonic() > deadline:
                    raise
                if not getattr(ws, "connected", True):
                    raise
                continue
            if message.get("id") == mid and message.get("type") == "result":
                if message.get("success") is not True:
                    raise _rejected(message.get("error"))
                return message.get("result")

    return request


def _exposed_ids(request) -> Dict[str, Any]:
    """Home Assistant answers this only for an administrator's token."""
    try:
        return (request("homeassistant/expose_entity/list") or {}).get("exposed_entities") or {}
    except HomeAssistantCommandRejected as exc:
        if exc.code == "unauthorized":
            raise HomeAssistantAuthError(ADMIN_MESSAGE) from None
        raise


def _load(request) -> List[Device]:
    """Read everything the device list needs through ``request(kind, **fields)``."""

    def optional(kind: str, default, **extra):
        try:
            return request(kind, **extra)
        except HomeAssistantCommandRejected:
            return default

    states = request("get_states") or []
    config = optional("get_config", {}) or {}
    entities = request("config/entity_registry/list") or []
    devices = request("config/device_registry/list") or []
    areas = request("config/area_registry/list") or []
    exposed = _exposed_ids(request)
    exposed_ids = [eid for eid, settings in exposed.items()
                   if isinstance(settings, dict) and settings.get("conversation") is True]
    extended = optional("config/entity_registry/get_entries", {}, entity_ids=exposed_ids) if exposed_ids else {}
    unit = ((config.get("unit_system") or {}).get("temperature") or "") if isinstance(config, dict) else ""
    return build_devices(states, entities, devices, areas, exposed, extended if isinstance(extended, dict) else {},
                         str(unit))


def test_connection(url: str, token: str, *, verify_tls: bool = True) -> int:
    """Connect once, read the device list and close, all within ``LOAD_TIMEOUT_SEC``: the number of exposed
    devices Jarvis can use."""
    if not token:
        raise HomeAssistantAuthError("Paste a Home Assistant access token first.")
    deadline = time.monotonic() + LOAD_TIMEOUT_SEC
    ws = _open(url, token, verify_tls, LOAD_TIMEOUT_SEC)
    try:
        found = _load(_sync_requester(ws, itertools.count(1), deadline))
    finally:
        _close(ws)
    debug_log(f"Home Assistant test: {len(found)} exposed devices", _TAG)
    return len(found)


class HomeAssistantConnector:
    """A long-lived connection that keeps ``registry`` in step with Home Assistant."""

    def __init__(self, registry: Registry, url: str, token: str, *, verify_tls: bool = True,
                 sleep: Callable[[float], None] = None):
        self._registry = registry
        self._url = url
        self._token = token
        self._verify_tls = verify_tls
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._send_lock = threading.Lock()
        self._pending: Dict[int, Tuple[threading.Event, list]] = {}
        self._pending_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._sleep = sleep or self._stop.wait
        self._reload_lock = threading.Lock()
        self._reload_wanted = threading.Event()
        self.connected = False
        # starting | connected | auth | unreachable | not_found | refused | missing_library | stopped
        self.status = "starting"

    def start(self) -> None:
        """Start the reader thread. Raises ``ImportError`` when websocket-client is not installed."""
        if self._thread is not None:
            return
        try:
            _websocket()
        except ImportError:
            self.status = "missing_library"
            debug_log("Home Assistant connector not started: websocket-client missing", _TAG)
            raise
        debug_log("Home Assistant connector starting", _TAG)
        self._thread = threading.Thread(target=self._run, name="smarthome-ha", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            _close(ws)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self.connected = False
        self.status = "stopped"
        debug_log("Home Assistant connector stopped", _TAG)

    # -- reader thread ------------------------------------------------------------------------

    def _run(self) -> None:
        delay = BACKOFF_START_SEC
        while not self._stop.is_set():
            try:
                self._session()
                delay = BACKOFF_START_SEC
            except HomeAssistantAuthError:
                self.status = "auth"
                debug_log("Home Assistant connection failed: token refused", _TAG)
            except HomeAssistantNotFound:
                self.status = "not_found"
                debug_log("Home Assistant connection failed: address not found yet", _TAG)
            except HomeAssistantUnreachable:
                self.status = "unreachable"
                debug_log("Home Assistant connection failed: unreachable", _TAG)
            except HomeAssistantError:
                self.status = "refused"
                debug_log("Home Assistant connection failed: refused", _TAG)
            except Exception as exc:  # noqa: BLE001 - the reader thread never dies on a bad message
                self.status = "unreachable"
                debug_log(f"Home Assistant connection failed: {type(exc).__name__}", _TAG)
            self._drop()
            if self._stop.is_set():
                break
            self._sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_SEC)

    def _drop(self) -> None:
        was = self.connected
        self.connected = False
        ws, self._ws = self._ws, None
        if ws is not None:
            _close(ws)
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for event, slot in pending:
            slot.append(None)
            event.set()
        if was:
            self._registry.mark_unavailable(SOURCE)

    def _session(self) -> None:
        deadline = time.monotonic() + LOAD_TIMEOUT_SEC
        ws = _open(self._url, self._token, self._verify_tls, LOAD_TIMEOUT_SEC)
        self._ws = ws
        if self._stop.is_set():
            return
        devices = _load(_sync_requester(ws, self._ids, deadline))
        self._registry.replace_source(SOURCE, devices)
        states_sub, registry_sub = next(self._ids), next(self._ids)
        ws.send(json.dumps({"id": states_sub, "type": "subscribe_events", "event_type": "state_changed"}))
        ws.send(json.dumps({"id": registry_sub, "type": "subscribe_events", "event_type": "entity_registry_updated"}))
        ws.settimeout(min(1.0, PING_AFTER_SEC))
        self.connected = True
        self.status = "connected"
        debug_log(f"Home Assistant connected: {len(devices)} devices", _TAG)
        websocket = _websocket()
        last_heard = time.monotonic()
        unanswered = 0
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if time.monotonic() - last_heard > PING_AFTER_SEC:
                    if unanswered >= MAX_MISSED_PINGS:
                        raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE)
                    self._send({"id": next(self._ids), "type": "ping"})
                    unanswered += 1
                    last_heard = time.monotonic()
                continue
            except (OSError, websocket.WebSocketException):
                raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE) from None
            if not raw:
                raise HomeAssistantUnreachable(UNREACHABLE_MESSAGE)
            last_heard = time.monotonic()
            unanswered = 0
            try:
                message = _parse(raw)
            except HomeAssistantError:
                continue
            self._dispatch(message, states_sub, registry_sub)

    def _dispatch(self, message: dict, states_sub: int, registry_sub: int) -> None:
        if message.get("type") == "event" and message.get("id") == registry_sub:
            self._schedule_reload()
            return
        if message.get("type") == "event" and message.get("id") == states_sub:
            data = (message.get("event") or {}).get("data") or {}
            entity_id = data.get("entity_id")
            new_state = data.get("new_state")
            if not isinstance(entity_id, str):
                return
            device_id = f"ha:{entity_id}"
            if not isinstance(new_state, dict):
                self._registry.update_state(device_id, UNAVAILABLE)
                return
            attributes = new_state.get("attributes") if isinstance(new_state.get("attributes"), dict) else {}
            self._registry.update_state(device_id, str(new_state.get("state") or ""),
                                        {k: attributes[k] for k in _KEPT_ATTRIBUTES if k in attributes})
            return
        if message.get("type") in ("result", "pong"):
            with self._pending_lock:
                waiter = self._pending.pop(message.get("id"), None)
            if waiter is not None:
                waiter[1].append(message)
                waiter[0].set()

    def _schedule_reload(self) -> None:
        """An entity's registry entry changed (Expose, name, area): read the device list again, off the reader
        thread, coalescing bursts of events into one reload."""
        self._reload_wanted.set()
        if not self._reload_lock.acquire(blocking=False):
            return

        def reload() -> None:
            try:
                while self._reload_wanted.is_set() and not self._stop.is_set():
                    self._reload_wanted.clear()
                    devices = _load(self._request)
                    self._registry.replace_source(SOURCE, devices)
                    debug_log(f"Home Assistant devices reloaded: {len(devices)}", _TAG)
            except HomeAssistantError as exc:
                debug_log(f"Home Assistant reload failed ({type(exc).__name__})", _TAG)
            finally:
                self._reload_lock.release()

        threading.Thread(target=reload, name="smarthome-ha-reload", daemon=True).start()

    # -- requests from other threads -------------------------------------------------------------

    def _send(self, message: dict) -> None:
        ws = self._ws
        if ws is None:
            raise HomeAssistantUnreachable(NOT_CONNECTED_MESSAGE)
        websocket = _websocket()
        with self._send_lock:
            try:
                ws.send(json.dumps(message))
            except (OSError, websocket.WebSocketException):
                raise HomeAssistantUnreachable(NOT_CONNECTED_MESSAGE) from None

    def _call(self, message: dict) -> dict:
        """Send one request and wait for its result (``COMMAND_TIMEOUT_SEC``); the reader thread delivers it."""
        mid = next(self._ids)
        event, slot = threading.Event(), []
        with self._pending_lock:
            self._pending[mid] = (event, slot)
        try:
            self._send({"id": mid, **message})
            if not event.wait(COMMAND_TIMEOUT_SEC):
                raise HomeAssistantUnreachable("Home Assistant did not confirm the command in time.")
        finally:
            with self._pending_lock:
                self._pending.pop(mid, None)
        reply = slot[0] if slot else None
        if reply is None:
            raise HomeAssistantUnreachable(NOT_CONNECTED_MESSAGE)
        return reply

    def _request(self, kind: str, **extra) -> Any:
        reply = self._call({"type": kind, **extra})
        if reply.get("success") is not True:
            raise _rejected(reply.get("error"))
        return reply.get("result")

    def send(self, device: Device, command: Command) -> Outcome:
        """Run one command on a device that is still exposed; returns once Home Assistant confirms it."""
        if not self.connected:
            raise HomeAssistantUnreachable(NOT_CONNECTED_MESSAGE)
        try:
            domain, service, data = service_call(device, command)
        except ValueError:
            raise HomeAssistantCommandRejected("That device cannot do that.") from None
        entity_id = device.id.split(":", 1)[1]
        settings = _exposed_ids(self._request).get(entity_id)
        if not isinstance(settings, dict) or settings.get("conversation") is not True:
            self._registry.remove(device.id)
            debug_log(f"Home Assistant command refused: entity no longer exposed ({device.kind})", _TAG)
            raise HomeAssistantCommandRejected(
                "That device is no longer exposed to voice assistants in Home Assistant, so nothing was changed.")
        reply = self._call({"type": "call_service", "domain": domain, "service": service, "service_data": data,
                            "target": {"entity_id": [entity_id]}})
        if reply.get("success") is not True:
            raise _rejected(reply.get("error"))
        return Outcome(confirmed=True)
