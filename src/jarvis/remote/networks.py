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
