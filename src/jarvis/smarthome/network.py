"""Where a smart home hub may be: loopback, RFC 1918, IPv4 link-local or IPv6 link-local only.

A hostname is accepted only if every address it resolves to is allowed, and connectors connect to the
resolved address so a later DNS change cannot point them elsewhere. Messages never repeat the address.
"""
from __future__ import annotations

import ipaddress
import socket
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


@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str      # as the user typed it (for the Host header and TLS name)
    ip: str        # what is actually connected to
    port: int

    @property
    def ip_for_url(self) -> str:
        return f"[{self.ip}]" if ":" in self.ip else self.ip


def _allowed(address: ipaddress._BaseAddress) -> bool:
    return any(address.version == net.version and address in net for net in ALLOWED_NETWORKS)


def _resolve(host: str) -> List[str]:
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(RESOLVE_TIMEOUT_SEC)
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    finally:
        socket.setdefaulttimeout(previous)
    return sorted({info[4][0] for info in infos})


def validate_endpoint(value, *, default_port: int, schemes: Sequence[str] = ("http", "https")) -> Endpoint:
    """The endpoint to connect to for ``value`` (a URL, or a bare host for a broker), or
    ``SmartHomeAddressError``."""
    if not isinstance(value, str) or not value.strip():
        raise SmartHomeAddressError("No address is set.")
    text = value.strip()
    if "://" not in text:
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
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        candidates = [str(literal)]
    else:
        candidates = _resolve(host)
        if not candidates:
            raise SmartHomeAddressError("The address could not be found on this network.")
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            raise SmartHomeAddressError("The address is not valid.") from None
        if not _allowed(address):
            raise SmartHomeAddressError(
                "The address must be on your home network (a private address such as 192.168.x.x, or this PC).")
    if port is None:
        port = {"https": 443, "mqtts": 8883}.get(parts.scheme, default_port)
    if not 0 < port < 65536:
        raise SmartHomeAddressError("The port is not valid.")
    return Endpoint(parts.scheme, host, str(ipaddress.ip_address(candidates[0].split("%", 1)[0])), port)


def check_connect(ip: str) -> None:
    """Called by every connector right before it opens a socket."""
    if _CONNECT_GUARD is not None:
        _CONNECT_GUARD(ip)
