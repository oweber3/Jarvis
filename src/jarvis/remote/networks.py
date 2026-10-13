"""Which client addresses phone access answers: this PC, home networks and private VPNs only."""
from __future__ import annotations

import ipaddress
import socket
from typing import List

# Loopback, home LAN ranges, link-local, the shared address space VPNs such as Tailscale use, and IPv6
# unique-local addresses (Tailscale's IPv6 range is inside fc00::/7).
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10",
    "100.64.0.0/10",
    "fc00::/7",
))


def is_allowed_client(address) -> bool:
    """True when ``address`` (an IP literal, optionally IPv4-mapped IPv6) is on a private network."""
    if not isinstance(address, str):
        return False
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == net.version and ip in net for net in ALLOWED_NETWORKS)


# Local DNS suffixes a home router or mDNS gives the PC's name; a Tailscale MagicDNS name is
# ``<name>.<tailnet>.ts.net``.
_LOCAL_NAME_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain")


def pc_name() -> str:
    """This PC's host name in lower case, or ``""``. Read locally; nothing is sent on the network."""
    try:
        return socket.gethostname().lower()
    except OSError:
        return ""


def is_own_host(name: str, own_name: str) -> bool:
    """Whether ``name`` (a request's lower-case ``Host`` name) is one the phone app is opened by.

    A private or loopback address, ``localhost``, or this PC's name (``own_name``) bare, under a local DNS
    suffix or as a Tailscale MagicDNS name. Any other name may be a DNS name rebound to this PC.
    """
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return is_allowed_client(name)
    if not own_name or not name.startswith(own_name):
        return False
    suffix = name[len(own_name):]
    if suffix in ("", *_LOCAL_NAME_SUFFIXES):
        return True
    return suffix.startswith(".") and suffix.endswith(".ts.net") and suffix.count(".") == 3


def local_addresses() -> List[str]:
    """This PC's private IPv4 addresses (home network and VPN adapters), for showing the phone link.

    Read from the local host name only; nothing is sent on the network.
    """
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    found: List[str] = []
    for info in infos:
        address = info[4][0]
        if address not in found and is_allowed_client(address) and not address.startswith(("127.", "169.254.")):
            found.append(address)
    return found
