"""A fake Home Assistant WebSocket API on loopback, for connector and tool tests.

Implements the parts of ``/api/websocket`` Jarvis uses: ``auth``, ``get_states``, ``get_config``, the
entity, device and area registries, ``config/entity_registry/get_entries``, the exposed-entity list,
``subscribe_events`` for ``state_changed`` and ``call_service``. Standard library only (RFC 6455 text
frames). Never reachable from outside this machine.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import socket
import struct
import threading
import time
from typing import Any, Dict, List, Optional

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# What each service does to the state in this fake.
_SERVICE_STATE = {
    "turn_on": "on", "turn_off": "off", "open_cover": "open", "close_cover": "closed", "lock": "locked",
    "unlock": "unlocked", "alarm_arm_away": "armed_away", "alarm_disarm": "disarmed",
}


def entity(entity_id: str, name: str, state: str = "off", *, area: str = "", exposed: bool = True,
           aliases=(), attributes: Optional[dict] = None, unique_id: str = "", platform: str = "demo",
           device_area: str = "") -> dict:
    """One entity as the fake serves it. ``area`` is the entity's own area, ``device_area`` its device's."""
    return {"entity_id": entity_id, "name": name, "state": state, "area": area, "exposed": exposed,
            "aliases": list(aliases), "attributes": dict(attributes or {}), "unique_id": unique_id,
            "platform": platform, "device_area": device_area}


class FakeHomeAssistant:
    def __init__(self, entities: List[dict], token: str = "good-token", *, temperature_unit: str = "°C"):
        self.token = token
        self.temperature_unit = temperature_unit
        self.entities: Dict[str, dict] = {e["entity_id"]: copy.deepcopy(e) for e in entities}
        self.calls: List[dict] = []
        self.received_types: List[str] = []
        self.redirect = False
        self.reject_with: Optional[str] = None
        self.no_result_for_calls = False
        self.answer_pings = True
        self.admin = True
        self.stall_after_auth = False
        self.handshake_byte_delay = 0.0   # answer the upgrade one byte at a time, each read well inside a timeout
        self.handshake_headers: List[Dict[str, str]] = []
        self._lock = threading.Lock()
        self._clients: List[_Client] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, name="fake-ha-accept", daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- test controls -------------------------------------------------------------------------

    def set_state(self, entity_id: str, state: str, attributes: Optional[dict] = None) -> None:
        """Change a state as if a person used the device, and tell subscribers."""
        with self._lock:
            ent = self.entities[entity_id]
            ent["state"] = state
            ent["attributes"].update(attributes or {})
            new_state = self._state(ent)
        self._broadcast(entity_id, new_state)

    def set_exposed(self, entity_id: str, exposed: bool, *, announce: bool = True) -> None:
        """Expose or un-expose an entity, as Settings > Voice assistants > Expose does, and tell subscribers
        to the entity registry."""
        with self._lock:
            self.entities[entity_id]["exposed"] = exposed
            clients = [c for c in self._clients if c.registry_subscription is not None] if announce else []
        for client in clients:
            try:
                client.send({"id": client.registry_subscription, "type": "event", "event": {
                    "event_type": "entity_registry_updated",
                    "data": {"action": "update", "entity_id": entity_id, "changes": {"options": {}}}}})
            except OSError:
                pass

    def drop_connections(self) -> None:
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.close()

    def connection_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def close(self) -> None:
        self._closed = True
        self.drop_connections()
        try:
            self._server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._server.close()
        except OSError:
            pass

    # -- server ----------------------------------------------------------------------------------

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="fake-ha-client", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        client = _Client(conn)
        try:
            headers = client.handshake(self)
        except (OSError, ValueError):
            client.close()
            return
        if headers is None:
            return
        with self._lock:
            self._clients.append(client)
        try:
            client.send({"type": "auth_required", "ha_version": "2026.11.0"})
            auth = client.recv()
            if not auth or auth.get("type") != "auth" or auth.get("access_token") != self.token:
                client.send({"type": "auth_invalid", "message": "Invalid access token or password"})
                client.close()
                return
            client.send({"type": "auth_ok", "ha_version": "2026.11.0"})
            while True:
                message = client.recv()
                if message is None:
                    return
                self._handle(client, message)
        except (OSError, ValueError):
            return
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)
            client.close()

    def _state(self, ent: dict) -> dict:
        attributes = dict(ent["attributes"])
        attributes.setdefault("friendly_name", ent["name"])
        return {"entity_id": ent["entity_id"], "state": ent["state"], "attributes": attributes}

    def _result(self, client, mid, result=None, error: Optional[str] = None) -> None:
        if error is not None:
            client.send({"id": mid, "type": "result", "success": False,
                         "error": {"code": "home_assistant_error", "message": error}})
        else:
            client.send({"id": mid, "type": "result", "success": True, "result": result})

    def _handle(self, client: "_Client", message: dict) -> None:
        kind, mid = message.get("type"), message.get("id")
        self.received_types.append(kind)
        with self._lock:
            ents = [copy.deepcopy(e) for e in self.entities.values()]
        if self.stall_after_auth:
            return
        if kind == "ping":
            if self.answer_pings:
                client.send({"id": mid, "type": "pong"})
        elif kind == "get_states":
            self._result(client, mid, [self._state(e) for e in ents])
        elif kind == "get_config":
            self._result(client, mid, {"unit_system": {"temperature": self.temperature_unit}})
        elif kind == "config/entity_registry/list":
            self._result(client, mid, [{
                "entity_id": e["entity_id"], "name": None, "area_id": _area_id(e["area"]),
                "device_id": f"dev-{e['entity_id']}", "unique_id": e["unique_id"] or f"uid-{e['entity_id']}",
                "platform": e["platform"], "disabled_by": None, "hidden_by": None} for e in ents])
        elif kind == "config/entity_registry/get_entries":
            wanted = set(message.get("entity_ids") or [])
            self._result(client, mid, {e["entity_id"]: {"entity_id": e["entity_id"], "aliases": e["aliases"]}
                                       for e in ents if e["entity_id"] in wanted})
        elif kind == "config/device_registry/list":
            self._result(client, mid, [{"id": f"dev-{e['entity_id']}", "area_id": _area_id(e["device_area"]),
                                        "name": "Device", "name_by_user": None} for e in ents])
        elif kind == "config/area_registry/list":
            areas = {a for e in ents for a in (e["area"], e["device_area"]) if a}
            self._result(client, mid, [{"area_id": _area_id(a), "name": a, "aliases": []} for a in sorted(areas)])
        elif kind == "homeassistant/expose_entity/list" and not self.admin:
            client.send({"id": mid, "type": "result", "success": False,
                         "error": {"code": "unauthorized", "message": "Unauthorized"}})
        elif kind == "homeassistant/expose_entity/list":
            self._result(client, mid, {"exposed_entities": {
                e["entity_id"]: {"conversation": e["exposed"], "cloud.alexa": False} for e in ents}})
        elif kind == "subscribe_events":
            if message.get("event_type") == "entity_registry_updated":
                client.registry_subscription = mid
            else:
                client.subscription = mid
            self._result(client, mid, None)
        elif kind == "call_service":
            self.calls.append({k: message.get(k) for k in ("domain", "service", "service_data", "target")})
            if self.reject_with is not None:
                self._result(client, mid, error=self.reject_with)
                return
            if self.no_result_for_calls:
                return
            for entity_id in (message.get("target") or {}).get("entity_id") or []:
                self._apply(entity_id, message.get("service"), message.get("service_data") or {})
            self._result(client, mid, None)
        else:
            client.send({"id": mid, "type": "result", "success": False,
                         "error": {"code": "unknown_command", "message": "Unknown command."}})

    def _apply(self, entity_id: str, service: str, data: dict) -> None:
        with self._lock:
            ent = self.entities.get(entity_id)
            if ent is None:
                return
            state = _SERVICE_STATE.get(service)
            if service == "toggle":
                state = "off" if ent["state"] == "on" else "on"
            if state is not None:
                ent["state"] = state
            if "brightness_pct" in data:
                ent["attributes"]["brightness"] = round(data["brightness_pct"] * 255 / 100)
            if "position" in data:
                ent["attributes"]["current_position"] = data["position"]
            if "temperature" in data:
                ent["attributes"]["temperature"] = data["temperature"]
            if "percentage" in data:
                ent["attributes"]["percentage"] = data["percentage"]
            new_state = self._state(ent)
        self._broadcast(entity_id, new_state)

    def _broadcast(self, entity_id: str, new_state: dict) -> None:
        with self._lock:
            clients = [c for c in self._clients if c.subscription is not None]
        for client in clients:
            try:
                client.send({"id": client.subscription, "type": "event", "event": {
                    "event_type": "state_changed", "data": {"entity_id": entity_id, "new_state": new_state}}})
            except OSError:
                pass


def _area_id(name: str) -> Optional[str]:
    return name.casefold().replace(" ", "_") if name else None


class _Client:
    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.subscription = None
        self.registry_subscription = None
        self._send_lock = threading.Lock()
        self._buffer = b""

    def handshake(self, server: FakeHomeAssistant) -> Optional[dict]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise ValueError("closed")
            data += chunk
        head, _, self._buffer = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        server.handshake_headers.append(headers)
        if server.redirect or not lines[0].startswith("GET /api/websocket "):
            self.conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://93.184.216.34/api/websocket\r\n"
                              b"Content-Length: 0\r\n\r\n")
            self.close()
            return None
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + _GUID).encode()).digest()).decode()
        answer = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
        if server.handshake_byte_delay:
            for index in range(len(answer)):
                time.sleep(server.handshake_byte_delay)
                self.conn.sendall(answer[index:index + 1])
        else:
            self.conn.sendall(answer)
        return headers

    def _read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise OSError("closed")
            self._buffer += chunk
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def recv(self) -> Optional[dict]:
        while True:
            first, second = self._read(2)
            opcode, masked, length = first & 0x0F, second & 0x80, second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else b"\x00\x00\x00\x00"
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(length)))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))

    def _frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        if len(payload) < 126:
            header += bytes([len(payload)])
        elif len(payload) < 65536:
            header += bytes([126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([127]) + struct.pack(">Q", len(payload))
        with self._send_lock:
            self.conn.sendall(header + payload)

    def send(self, message: Any) -> None:
        self._frame(0x1, json.dumps(message).encode("utf-8"))

    def close(self) -> None:
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass
