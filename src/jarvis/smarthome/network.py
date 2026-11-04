"""Where a smart home hub may be: loopback, RFC 1918, IPv4 link-local or IPv6 link-local only.

A hostname is accepted only if every address it resolves to is allowed, and connectors connect to the
resolved address so a later DNS change cannot point them elsewhere. Messages never repeat the address.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence
from urllib.parse import urlsplit

# The Roku rule (devices/roku.py) plus loopback: a Home Assistant virtual machine may sit behind a
# forwarded port on this PC.
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fe80::/10",
    "127.0.0.0/8", "::1/128"))
RESOLVE_TIMEOUT_SEC = 3.0

# Tests set this to refuse anything but loopback; it receives the IP about to be connected to.
_CONNECT_GUARD: Optional[Callable[[str], None]] = None


class SmartHomeAddressError(ValueError):
    """The configured hub address is not on the local network, or is not an address at all."""


class SmartHomeNotFound(SmartHomeAddressError):
    """A hostname that does not resolve right now (the network or the hub may not be up yet)."""


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str      # as the user typed it (for the Host header and TLS name)
    ip: str        # what is actually connected to
    port: int

    @property
    def host_for_url(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host


def _allowed(address: ipaddress._BaseAddress) -> bool:
    return any(address.version == net.version and address in net for net in ALLOWED_NETWORKS)


def _resolve(host: str, timeout: float) -> List[str]:
    """Every address ``host`` resolves to, within ``timeout`` seconds; [] when none or too slow.

    The lookup runs on its own daemon thread so a slow resolver cannot hold the caller, and the process-wide
    socket timeout is never touched (connectors resolve concurrently). A link-local address keeps its scope."""
    found: List[str] = []
    done = threading.Event()

    def lookup() -> None:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            found.extend(sorted({str(info[4][0]) for info in infos}))
        except (OSError, UnicodeError):
            pass
        finally:
            done.set()

    threading.Thread(target=lookup, name="smarthome-resolve", daemon=True).start()
    return list(found) if done.wait(timeout) else []


def validate_endpoint(value, *, default_port: int, schemes: Sequence[str] = ("http", "https"),
                      resolve: bool = True, resolve_timeout: Optional[float] = None) -> Endpoint:
    """The endpoint to connect to for ``value`` (a URL, or a bare host for a broker), or
    ``SmartHomeAddressError`` (``SmartHomeNotFound`` for a hostname that does not resolve now).

    With ``resolve`` false only the syntax and a literal address are checked (``ip`` is empty for a
    hostname): connectors resolve inside their retry loops. A lookup waits at most ``resolve_timeout``
    (``RESOLVE_TIMEOUT_SEC`` unless given)."""
    if not isinstance(value, str) or not value.strip():
        raise SmartHomeAddressError("No address is set.")
    text = value.strip()
    typed_scheme = "://" in text
    if not typed_scheme:
        text = f"{schemes[0]}://{text}"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise SmartHomeAddressError("The address is not valid.") from None
    if parts.scheme not in schemes:
        raise SmartHomeAddressError(f"The address must start with {' or '.join(s + '://' for s in schemes)}.")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise SmartHomeAddressError("Enter only the address and port, with no path or login.")
    host = parts.hostname or ""
    if not host or "%" in host or any(c.isspace() for c in host):
        raise SmartHomeAddressError("The address is not valid.")
    if port is None:
        # A typed "http://" without a port is the web port, as in a browser (which hides ":80");
        # a bare address uses the service's usual port.
        port = {"https": 443, "mqtts": 8883, **({"http": 80} if typed_scheme else {})}.get(parts.scheme,
                                                                                          default_port)
    if not 0 < port < 65536:
        raise SmartHomeAddressError("The port is not valid.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        candidates = [str(literal)]
    elif not resolve:
        return Endpoint(parts.scheme, host, "", port)
    else:
        candidates = _resolve(host, RESOLVE_TIMEOUT_SEC if resolve_timeout is None else resolve_timeout)
        if not candidates:
            raise SmartHomeNotFound("The address could not be found on this network yet.")
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            raise SmartHomeAddressError("The address is not valid.") from None
        if not _allowed(address):
            raise SmartHomeAddressError(
                "The address must be on your home network (a private address such as 192.168.x.x, or this PC).")
    return Endpoint(parts.scheme, host, candidates[0], port)


def check_connect(ip: str) -> None:
    """Called by every connector right before it opens a socket."""
    if _CONNECT_GUARD is not None:
        _CONNECT_GUARD(ip)
