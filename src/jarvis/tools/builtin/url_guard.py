"""SSRF guard shared by the tools that fetch URLs chosen by a model or a web page.

A URL is public only when its scheme is http(s), the standard library and the
parser requests uses (urllib3) agree on its host, and every address the host
resolves to is globally routable and not one of this PC's own interface
addresses. ``fetch_public`` resolves each hop once,
checks every address, and connects to the checked address itself, so a second
DNS answer can never move the connection somewhere else. TLS still sends and
verifies the original host name. Redirects are walked one hop at a time so
each hop is checked too.
"""

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import parse_url

from ...debug import debug_log

# Max redirects to follow manually (so each hop can be re-validated).
MAX_REDIRECTS = 3
# Most bytes read from one page. Caps memory and prompt-injection surface.
MAX_FETCH_BYTES = 512 * 1024

_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# How long the list of this PC's own interface addresses is reused before it is read again.
_LOCAL_ADDRESS_TTL_SEC = 30.0

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

_local_lock = threading.Lock()
_local_cache: Tuple[float, FrozenSet[IPAddress]] = (float("-inf"), frozenset())


class NonPublicUrlError(ValueError):
    """The URL, or a redirect it led to, is not a public http(s) address."""


def _embedded_ipv4(ip: IPAddress) -> List[ipaddress.IPv4Address]:
    """The IPv4 addresses an IPv6 address carries (mapped, NAT64, 6to4, Teredo)."""
    if ip.version != 6:
        return []
    embedded = [ip.ipv4_mapped, ip.sixtofour]
    if ip.teredo:
        embedded.extend(ip.teredo)
    if ip in _NAT64:
        embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return [inner for inner in embedded if inner is not None]


def is_public_address(ip: IPAddress) -> bool:
    """True only for a globally routable unicast address.

    IPv6 forms that carry an IPv4 address (mapped, NAT64, 6to4, Teredo) are
    judged by that address too, and the deprecated IPv4-compatible,
    IPv4-translated and site-local ranges are never public.
    """
    if ip.version == 6:
        if ip in _IPV4_COMPATIBLE or ip in _IPV4_TRANSLATED or ip.is_site_local:
            return False
        if any(not is_public_address(inner) for inner in _embedded_ipv4(ip)):
            return False
    return ip.is_global and not ip.is_multicast


def _interface_addresses() -> FrozenSet[IPAddress]:
    """Every address this PC's network interfaces have, read locally."""
    found = set()
    try:
        import psutil
        raw = [entry.address for entries in psutil.net_if_addrs().values() for entry in entries
               if entry.family in (socket.AF_INET, socket.AF_INET6)]
    except Exception as e:
        debug_log(f"Interface list unavailable ({type(e).__name__}); using this PC's host name", "web")
        try:
            raw = [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)]
        except OSError:
            raw = []
    for address in raw:
        try:
            found.add(ipaddress.ip_address(str(address).split("%", 1)[0]))
        except ValueError:
            continue
    return frozenset(found)


def _local_addresses() -> FrozenSet[IPAddress]:
    """This PC's own addresses, read again once they are older than ``_LOCAL_ADDRESS_TTL_SEC``."""
    global _local_cache
    with _local_lock:
        stamp, addresses = _local_cache
        now = time.monotonic()
        if now - stamp >= _LOCAL_ADDRESS_TTL_SEC:
            addresses = _interface_addresses()
            _local_cache = (now, addresses)
        return addresses


def _is_allowed(ip: IPAddress) -> bool:
    """A public address that is not one of this PC's own (a global IPv6 or public IPv4 on an interface
    reaches services listening on every address)."""
    if not is_public_address(ip):
        return False
    local = _local_addresses()
    if ip in local or any(inner in local for inner in _embedded_ipv4(ip)):
        debug_log("Refusing a fetch target that is one of this PC's own addresses", "web")
        return False
    return True


@dataclass(frozen=True)
class _Target:
    url: str          # the URL exactly as requests will send it
    scheme: str
    host: str         # lower-case ASCII host, no brackets
    host_header: str  # the Host header the original URL implies
    address: str      # the checked address the connection goes to


def _agreed_host(url: str) -> Optional[str]:
    """The host both URL parsers see, or ``None`` when they disagree or the URL is not http(s).

    The standard library and urllib3 split some URLs differently (a backslash
    ends urllib3's authority but not ``urlsplit``'s), so a URL is only
    accepted when there is no doubt about which host it names.
    """
    try:
        ours = urlsplit(url)
        theirs = parse_url(url)
        ours_port, theirs_port = ours.port, theirs.port
    except Exception:
        return None
    scheme = (ours.scheme or "").lower()
    if scheme not in ("http", "https") or (theirs.scheme or "").lower() != scheme:
        return None
    if "\\" in ours.netloc:
        return None
    ours_host = ours.hostname or ""
    theirs_host = (theirs.host or "").strip("[]").lower()
    if not ours_host or not theirs_host or ours_port != theirs_port:
        return None
    try:
        ours_ascii = ours_host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return theirs_host if ours_ascii == theirs_host else None


def _resolve_public(host: str, port: int) -> Optional[str]:
    """The address to connect to when every address ``host`` has is public, else ``None``."""
    try:
        return host if _is_allowed(ipaddress.ip_address(host)) else None
    except ValueError:
        pass
    # Hostname: resolve all addresses and reject if any is non-public. This
    # is stricter than checking only the first A record: a hostile DNS could
    # return [1.1.1.1, 127.0.0.1] and some clients would try both.
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception as e:
        debug_log(f"DNS lookup failed for a fetch target: {type(e).__name__}", "web")
        return None
    addresses = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except (ValueError, IndexError, TypeError):
            return None
        if not _is_allowed(ip):
            debug_log("Refusing a fetch target that resolves to a non-public address", "web")
            return None
        addresses.append(str(ip))
    return addresses[0] if addresses else None


def _target(url: str) -> Optional[_Target]:
    host = _agreed_host(url)
    if host is None:
        return None
    try:
        prepared = requests.Request("GET", url).prepare()
        sent = parse_url(prepared.url)
    except Exception:
        return None
    sent_host = (sent.host or "").strip("[]").lower()
    if sent_host != host:
        return None
    scheme = sent.scheme.lower()
    port = sent.port or (443 if scheme == "https" else 80)
    address = _resolve_public(host, port)
    if address is None:
        return None
    shown = f"[{host}]" if ":" in host else host
    host_header = shown if sent.port is None else f"{shown}:{sent.port}"
    return _Target(url=prepared.url, scheme=scheme, host=host, host_header=host_header, address=address)


def is_public_url(url: str) -> bool:
    """Reject non-http(s) URLs, ambiguous URLs and URLs whose host has any non-public address.

    Defence against SSRF: search results, page text or a redirect chain could
    point at 127.0.0.1, 169.254.169.254 (cloud metadata), 10.x/192.168.x,
    100.64.x (Tailscale and other shared address space) or file:///etc/passwd.
    """
    return _target(url) is not None


class _PinnedAdapter(HTTPAdapter):
    """Connects to one checked address while TLS sends and verifies the original host name."""

    def __init__(self, target: _Target) -> None:
        self._target = target
        super().__init__(max_retries=0)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = self._target.address
        if host_params.get("scheme") == "https":
            pool_kwargs["server_hostname"] = self._target.host
        return host_params, pool_kwargs


def _get_pinned(target: _Target, headers: Optional[Dict[str, str]], timeout: float,
                stream: bool) -> "requests.Response":
    session = requests.Session()
    # System proxies would resolve the name themselves and undo the pinning.
    session.trust_env = False
    adapter = _PinnedAdapter(target)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    hop_headers = dict(headers or {})
    hop_headers["Host"] = target.host_header
    return session.get(target.url, headers=hop_headers, timeout=timeout,
                       allow_redirects=False, stream=stream)


def fetch_public(url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 15.0, stream: bool = False) -> "requests.Response":
    """GET ``url``, re-validating the initial URL and every redirect hop.

    Each hop is resolved once and the request goes to the address that was
    checked. Returns the final (non-redirect) response, open and unconsumed.
    Raises ``NonPublicUrlError`` before any request fires for a non-public or
    ambiguous URL or redirect target, and for a chain longer than
    ``MAX_REDIRECTS``.
    """
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        target = _target(current_url)
        if target is None:
            raise NonPublicUrlError("Refusing to fetch a non-public address.")
        response = _get_pinned(target, headers, timeout, stream)
        if not (response.is_redirect or response.is_permanent_redirect):
            return response
        location = response.headers.get("Location", "")
        response.close()
        if not location:
            raise NonPublicUrlError("Redirect without a target.")
        current_url = urljoin(target.url, location)
        debug_log("Following a redirect; the next hop is checked before it is requested", "web")
    raise NonPublicUrlError("Too many redirects.")


def read_capped(response: "requests.Response", max_bytes: int = MAX_FETCH_BYTES) -> bytes:
    """Read at most ``max_bytes`` of a streamed response body, then stop."""
    chunks: list = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            debug_log(f"Response body cut off at {max_bytes} bytes", "web")
            break
    return b"".join(chunks)[:max_bytes]
