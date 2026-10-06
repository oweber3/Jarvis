"""Roku External Control Protocol (ECP) client and SSDP discovery.

Local network only: the host must be a private or link-local IP literal (``ALLOWED_NETWORKS``), requests
never follow redirects or use system proxy settings, and every call is time-bounded. Nothing here knows
about tools, the reply engine or the LLM. See ``roku.spec.md``.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar
from urllib.parse import quote, urlparse

import requests

from ..debug import debug_log

ECP_PORT = 8060
TIMEOUT_SEC = 2.0
MAX_BODY_BYTES = 256 * 1024
MAX_REPEAT = 10
MAX_TEXT_CHARS = 100
REPEAT_PAUSE_SEC = 0.05
SSDP_TARGET = ("239.255.255.250", 1900)
SEARCH_INTERVAL_SEC = 60.0

# RFC 1918, IPv4 link-local and IPv6 link-local. Anything else is not a device on this network.
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fe80::/10"))

# Keys of the ECP remote. Letters are sent through ``type_text`` as ``Lit_`` keypresses, never by name.
KEYS = (
    "Home", "Back", "Select", "Up", "Down", "Left", "Right", "Play", "Rev", "Fwd", "InstantReplay", "Info",
    "Search", "Enter", "Backspace", "ChannelUp", "ChannelDown",
    "VolumeUp", "VolumeDown", "VolumeMute", "PowerOn", "PowerOff",
    "InputTuner", "InputHDMI1", "InputHDMI2", "InputHDMI3", "InputHDMI4", "InputAV1",
)
_KEYS_BY_FOLD = {key.casefold(): key for key in KEYS}
_APP_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Tests set this to refuse real devices; it receives the HTTP method and the host.
_SEND_GUARD: Optional[Callable[[str, str], None]] = None


class RokuError(Exception):
    """Base for every failure the client reports."""


class RokuAddressError(RokuError, ValueError):
    """The configured host is not a private or link-local IP literal."""


class RokuUnreachable(RokuError):
    """The device did not answer in time, or refused the connection."""


class RokuForbidden(RokuError):
    """The device answered 403: Control by mobile apps is limited or disabled."""


class RokuRequestFailed(RokuError):
    """The request was invalid, or the device answered something unusable."""


@dataclass(frozen=True)
class App:
    id: Optional[str]
    name: str
    kind: str = ""


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    model: str
    serial: str
    power_mode: str
    supports_text: bool


@dataclass(frozen=True)
class Discovered:
    host: str
    serial: str


def validate_host(value) -> str:
    """The canonical text of a private or link-local IP literal, or ``RokuAddressError``."""
    if not isinstance(value, str):
        raise RokuAddressError("The TV address must be an IP address.")
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text or "%" in text:
        raise RokuAddressError("The TV address must be an IP address on your home network.")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise RokuAddressError("The TV address must be an IP address on your home network.") from None
    if not any(address.version == net.version and address in net for net in ALLOWED_NETWORKS):
        raise RokuAddressError("The TV address must be a private or link-local network address.")
    return str(address)


def resolve_key(name) -> Optional[str]:
    """The canonical ECP key for a name (any case), or ``None``."""
    return _KEYS_BY_FOLD.get(name.strip().casefold()) if isinstance(name, str) else None


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold(), re.UNICODE)


def resolve_app(query: str, apps: list[App]) -> tuple[Optional[App], list[App]]:
    """One app for a spoken name: exact name or id first, then a unique whole-token match.

    Returns ``(app, [])`` on success, ``(None, candidates)`` when several fit and ``(None, [])`` when
    none do. Never picks among several."""
    wanted = _tokens(query or "")
    if not wanted:
        return None, []
    exact = [app for app in apps if _tokens(app.name) == wanted or (app.id and app.id.casefold() == query.strip().casefold())]
    if len(exact) == 1:
        return exact[0], []
    if exact:
        return None, exact
    partial = [app for app in apps if set(wanted) <= set(_tokens(app.name))]
    if len(partial) == 1:
        return partial[0], []
    return None, partial


def _text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _parse(body: bytes) -> ET.Element:
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise RokuRequestFailed("The TV sent a response Jarvis does not accept.")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise RokuRequestFailed("The TV sent a response Jarvis could not read.") from None


def _app_from(node: ET.Element) -> Optional[App]:
    name = " ".join((node.text or "").split())
    app_id = node.get("id")
    if app_id is not None and not _APP_ID.match(app_id):
        return None
    return App(id=app_id, name=name, kind=node.get("type", "")) if name else None


class RokuClient:
    def __init__(self, host: str):
        self.host = validate_host(host)
        shown = f"[{self.host}]" if ":" in self.host else self.host
        self._base = f"http://{shown}:{ECP_PORT}"
        self._session = requests.Session()
        self._session.trust_env = False

    def __enter__(self) -> "RokuClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    # -- transport ----------------------------------------------------------------------------------

    def _send(self, method: str, path: str, *, read_body: bool) -> bytes:
        if _SEND_GUARD is not None:
            _SEND_GUARD(method, self.host)
        deadline = time.monotonic() + TIMEOUT_SEC
        try:
            response = self._session.request(method, self._base + path, timeout=TIMEOUT_SEC,
                                             allow_redirects=False, stream=True)
        except requests.exceptions.RequestException as exc:
            debug_log(f"Roku {method} failed ({type(exc).__name__}).", "tv")
            raise RokuUnreachable("The TV did not answer.") from None
        try:
            if response.status_code == 403:
                raise RokuForbidden("The TV refused the command (HTTP 403).")
            if not 200 <= response.status_code < 300:
                raise RokuRequestFailed(f"The TV answered HTTP {response.status_code}.")
            if not read_body:
                return b""
            body = bytearray()
            try:
                for chunk in response.iter_content(8192):
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise RokuRequestFailed("The TV sent a response that was too large.")
                    if time.monotonic() > deadline:
                        raise RokuUnreachable("The TV answered too slowly.")
            except requests.exceptions.RequestException as exc:
                debug_log(f"Roku read failed ({type(exc).__name__}).", "tv")
                raise RokuUnreachable("The TV did not finish answering.") from None
            return bytes(body)
        finally:
            response.close()

    def _get(self, path: str) -> bytes:
        return self._send("GET", path, read_body=True)

    def _post(self, path: str) -> None:
        self._send("POST", path, read_body=False)

    # -- queries ------------------------------------------------------------------------------------

    def device_info(self) -> DeviceInfo:
        root = _parse(self._get("/query/device-info"))
        name = _text(root, "user-device-name") or _text(root, "friendly-device-name") or _text(root, "model-name")
        return DeviceInfo(name=name, model=_text(root, "model-name"), serial=_text(root, "serial-number"),
                          power_mode=_text(root, "power-mode"),
                          supports_text=_text(root, "supports-ecs-textedit").casefold() == "true")

    def apps(self) -> list[App]:
        root = _parse(self._get("/query/apps"))
        return [app for app in map(_app_from, root.findall("app")) if app is not None and app.id]

    def active_app(self) -> Optional[App]:
        node = _parse(self._get("/query/active-app")).find("app")
        return _app_from(node) if node is not None else None

    # -- commands -----------------------------------------------------------------------------------

    def key(self, name, repeat: int = 1) -> None:
        canonical = resolve_key(name)
        if canonical is None:
            raise RokuRequestFailed("That is not a key the TV remote has.")
        try:
            count = max(1, min(int(repeat), MAX_REPEAT))
        except (TypeError, ValueError):
            count = 1
        for index in range(count):
            if index:
                time.sleep(REPEAT_PAUSE_SEC)
            self._post(f"/keypress/{canonical}")
        debug_log(f"Roku key {canonical} x{count}.", "tv")

    def launch(self, app_id) -> None:
        if not isinstance(app_id, str) or not _APP_ID.match(app_id):
            raise RokuRequestFailed("That is not a valid app id.")
        self._post(f"/launch/{app_id}")
        debug_log("Roku launch sent.", "tv")

    def type_text(self, text) -> None:
        if not isinstance(text, str) or not text:
            raise RokuRequestFailed("There is no text to type.")
        if len(text) > MAX_TEXT_CHARS or any(ord(char) < 32 for char in text):
            raise RokuRequestFailed(f"Text must be plain and at most {MAX_TEXT_CHARS} characters.")
        for char in text:
            self._post(f"/keypress/Lit_{quote(char, safe='')}")
        debug_log(f"Roku text sent ({len(text)} characters).", "tv")


def discover(timeout: float = 1.5, target: tuple[str, int] = SSDP_TARGET) -> list[Discovered]:
    """Rokus answering an SSDP ``roku:ecp`` search, on private addresses only, one per host."""
    request = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               "ST: roku:ecp\r\nMX: 1\r\n\r\n").encode()
    found: dict[str, Discovered] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.sendto(request, target)
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            reply = _parse_ssdp(data)
            if reply is not None:
                found.setdefault(reply.host, reply)
    except OSError as exc:
        debug_log(f"Roku discovery failed ({type(exc).__name__}).", "tv")
    finally:
        sock.close()
    debug_log(f"Roku discovery found {len(found)} device(s).", "tv")
    return list(found.values())


T = TypeVar("T")


class RokuDevice:
    """The configured TV: its remembered identity, a cached app list and recovery when its address moves.

    The app list is read from the TV (``apps``) and kept so callers that must not wait on the network
    (fast-path matching) can read ``cached_apps``. When the TV stops answering at ``host`` and its serial
    is known, a search of the network can find the same TV at a new address; a different Roku is never
    adopted."""

    def __init__(self, host: str):
        self.host = validate_host(host)
        self.current_host = self.host
        self.serial: Optional[str] = None
        self._apps: tuple[App, ...] = ()
        self._moved = False
        self._last_search = float("-inf")
        self._lock = threading.Lock()

    def cached_apps(self) -> tuple[App, ...]:
        return self._apps

    def take_move_notice(self) -> bool:
        """True once after the TV was found at a new address, so the caller can say so."""
        moved, self._moved = self._moved, False
        return moved

    def call(self, operation: Callable[[RokuClient], T]) -> T:
        """Run ``operation`` on a client for the TV, finding the TV again once if it does not answer."""
        try:
            with RokuClient(self.current_host) as client:
                return operation(client)
        except RokuUnreachable:
            if not self._relocate():
                raise
        with RokuClient(self.current_host) as client:
            return operation(client)

    def remember_apps(self, apps: list[App]) -> None:
        self._apps = tuple(apps)

    def apps(self) -> list[App]:
        """The installed apps, read from the TV now and remembered."""
        apps = self.call(lambda client: client.apps())
        self.remember_apps(apps)
        return apps

    def warm_up(self) -> None:
        """Read the identity and app list with GETs only; a TV that is off is not an error."""
        try:
            info = self.call(lambda client: client.device_info())
            self.serial = info.serial or self.serial
            self.apps()
            debug_log(f"Roku warm-up read {len(self._apps)} apps.", "tv")
        except RokuError as exc:
            debug_log(f"Roku warm-up skipped ({type(exc).__name__}).", "tv")

    def _relocate(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if not self.serial or now - self._last_search < SEARCH_INTERVAL_SEC:
                return False
            self._last_search = now
            for found in discover():
                if found.serial == self.serial and found.host != self.current_host:
                    self.current_host = found.host
                    self._moved = True
                    debug_log("Roku found again at a new address.", "tv")
                    return True
        return False


_devices: dict[str, RokuDevice] = {}
_devices_lock = threading.Lock()


def get_device(host: str) -> RokuDevice:
    """The one ``RokuDevice`` for a configured host."""
    canonical = validate_host(host)
    with _devices_lock:
        if canonical not in _devices:
            _devices[canonical] = RokuDevice(canonical)
        return _devices[canonical]


def reset_devices() -> None:
    """Forget every device (tests and config changes)."""
    with _devices_lock:
        _devices.clear()


def _parse_ssdp(data: bytes) -> Optional[Discovered]:
    headers = {}
    for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().casefold()] = value.strip()
    host = urlparse(headers.get("location", "")).hostname
    try:
        host = validate_host(host)
    except RokuAddressError:
        return None
    match = re.search(r"roku:ecp:(.+)$", headers.get("usn", ""))
    return Discovered(host=host, serial=match.group(1) if match else "")


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m jarvis.devices.roku --discover``: list Rokus on this network (read-only)."""
    import argparse
    parser = argparse.ArgumentParser(prog="python -m jarvis.devices.roku",
                                     description="Find Roku TVs on this network. Only reads; never presses a key.")
    parser.add_argument("--discover", action="store_true", help="search for Roku devices")
    args = parser.parse_args(argv)
    if not args.discover:
        parser.print_help()
        return 0
    print("📺 Looking for Roku devices on this network...")
    devices = discover()
    if not devices:
        print("  ❌ None answered. Check the TV is on and on the same network.")
        return 1
    for device in devices:
        label = ""
        try:
            with RokuClient(device.host) as client:
                info = client.device_info()
            label = f" ({info.name}, {info.model})"
        except RokuError:
            pass
        print(f"  ✅ {device.host}{label}")
    print("  💡 Put the address in config.json as \"roku_host\" and reserve it for the TV in your router.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
