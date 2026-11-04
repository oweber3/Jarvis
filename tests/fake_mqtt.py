"""An in-process fake MQTT 3.1.1 broker on loopback, for the MQTT connector and tool tests.

Supports what Jarvis uses: CONNECT with an optional login, SUBSCRIBE with ``+`` and ``#`` wildcards,
retained messages, QoS 0 PUBLISH in both directions, PINGREQ and DISCONNECT. ``devices`` are simulated
with ``publish`` (as a device would) and ``on_command`` (a device answering a command). Standard library
only; never reachable from outside this machine.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
from typing import Callable, Dict, List, Optional, Tuple


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for index, part in enumerate(p):
        if part == "#":
            return True
        if index >= len(t) or (part != "+" and part != t[index]):
            return False
    return len(p) == len(t)


def _encode_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte, length = length % 128, length // 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            return bytes(out)


def _string(data: bytes, offset: int) -> Tuple[str, int]:
    size = struct.unpack(">H", data[offset:offset + 2])[0]
    return data[offset + 2:offset + 2 + size].decode("utf-8"), offset + 2 + size


class FakeBroker:
    def __init__(self, username: str = "", password: str = ""):
        self.username, self.password = username, password
        self.retained: Dict[str, bytes] = {}
        self.published: List[Tuple[str, bytes, bool]] = []   # what clients published
        self.subscriptions_seen: List[str] = []
        self.logins: List[Tuple[str, str]] = []
        self.on_command: Optional[Callable[[str, bytes], None]] = None
        self._lock = threading.Lock()
        self._clients: List[_Session] = []
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        self._closed = False
        threading.Thread(target=self._accept, name="fake-mqtt-accept", daemon=True).start()

    # -- test controls ---------------------------------------------------------------------------

    def publish(self, topic: str, payload, retain: bool = True) -> None:
        """Publish as a device would (discovery messages and states are usually retained)."""
        data = payload if isinstance(payload, bytes) else (
            json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)).encode("utf-8")
        with self._lock:
            if retain:
                if data:
                    self.retained[topic] = data
                else:
                    self.retained.pop(topic, None)
            clients = list(self._clients)
        for client in clients:
            client.deliver(topic, data, retain=False)

    def connected_clients(self) -> int:
        with self._lock:
            return sum(1 for c in self._clients if c.connected)

    def drop_connections(self) -> None:
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.close()

    def close(self) -> None:
        self._closed = True
        self.drop_connections()
        for step in (lambda: self._server.shutdown(socket.SHUT_RDWR), self._server.close):
            try:
                step()
            except OSError:
                pass

    # -- server ----------------------------------------------------------------------------------

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            session = _Session(self, conn)
            with self._lock:
                self._clients.append(session)
            threading.Thread(target=session.serve, name="fake-mqtt-client", daemon=True).start()

    def _from_client(self, topic: str, data: bytes, retain: bool) -> None:
        self.published.append((topic, data, retain))
        if retain:
            with self._lock:
                self.retained[topic] = data
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.deliver(topic, data, retain=False)
        if self.on_command is not None:
            self.on_command(topic, data)

    def _forget(self, session: "_Session") -> None:
        with self._lock:
            if session in self._clients:
                self._clients.remove(session)


class _Session:
    def __init__(self, broker: FakeBroker, conn: socket.socket):
        self.broker, self.conn = broker, conn
        self.patterns: List[str] = []
        self.connected = False
        self._send_lock = threading.Lock()

    def _read_exact(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.conn.recv(size - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _packet(self) -> Tuple[int, int, bytes]:
        header = self._read_exact(1)[0]
        length, multiplier = 0, 1
        while True:
            byte = self._read_exact(1)[0]
            length += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        return header >> 4, header & 0x0F, self._read_exact(length)

    def _send(self, kind: int, flags: int, body: bytes) -> None:
        with self._send_lock:
            self.conn.sendall(bytes([(kind << 4) | flags]) + _encode_length(len(body)) + body)

    def deliver(self, topic: str, data: bytes, retain: bool) -> None:
        if not self.connected or not any(topic_matches(p, topic) for p in self.patterns):
            return
        encoded = topic.encode("utf-8")
        try:
            self._send(3, 0x01 if retain else 0, struct.pack(">H", len(encoded)) + encoded + data)
        except OSError:
            pass

    def serve(self) -> None:
        try:
            while True:
                kind, flags, body = self._packet()
                if kind == 1:
                    self._connect(body)
                elif kind == 3:
                    topic, offset = _string(body, 0)
                    qos = (flags >> 1) & 0x03
                    if qos:
                        packet_id = body[offset:offset + 2]
                        offset += 2
                        self._send(4, 0, packet_id)
                    self.broker._from_client(topic, body[offset:], bool(flags & 0x01))
                elif kind == 8:
                    self._subscribe(body)
                elif kind == 10:
                    self._send(11, 0, body[:2])
                elif kind == 12:
                    self._send(13, 0, b"")
                elif kind == 14:
                    return
        except OSError:
            return
        finally:
            self.broker._forget(self)
            self.close()

    def _connect(self, body: bytes) -> None:
        _name, offset = _string(body, 0)
        offset += 1  # protocol level
        flags = body[offset]
        offset += 3  # flags and keep-alive
        _client_id, offset = _string(body, offset)
        if flags & 0x04:  # will
            _topic, offset = _string(body, offset)
            _message, offset = _string(body, offset)
        username = password = ""
        if flags & 0x80:
            username, offset = _string(body, offset)
        if flags & 0x40:
            password, offset = _string(body, offset)
        self.broker.logins.append((username, password))
        if self.broker.username and (username, password) != (self.broker.username, self.broker.password):
            self._send(2, 0, b"\x00\x05")  # not authorised
            raise OSError("refused")
        self.connected = True
        self._send(2, 0, b"\x00\x00")

    def _subscribe(self, body: bytes) -> None:
        packet_id, offset, granted, new = body[:2], 2, bytearray(), []
        while offset < len(body):
            pattern, offset = _string(body, offset)
            offset += 1
            self.patterns.append(pattern)
            self.broker.subscriptions_seen.append(pattern)
            granted.append(0)
            new.append(pattern)
        self._send(9, 0, packet_id + bytes(granted))
        with self.broker._lock:
            retained = list(self.broker.retained.items())
        for topic, data in retained:
            if any(topic_matches(p, topic) for p in new):
                encoded = topic.encode("utf-8")
                try:
                    self._send(3, 0x01, struct.pack(">H", len(encoded)) + encoded + data)
                except OSError:
                    return

    def close(self) -> None:
        self.connected = False
        for step in (lambda: self.conn.shutdown(socket.SHUT_RDWR), self.conn.close):
            try:
                step()
            except OSError:
                pass
