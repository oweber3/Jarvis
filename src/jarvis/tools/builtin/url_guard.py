"""SSRF guard shared by the tools that fetch URLs chosen by a model or a web page.

A URL is public only when its scheme is http(s) and every address its host
resolves to is routable on the open internet. Tools that follow redirects walk
them one hop at a time through ``fetch_public`` so each hop is checked too.
"""

import ipaddress
import socket
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests

from ...debug import debug_log

# Max redirects to follow manually (so each hop can be re-validated).
MAX_REDIRECTS = 3


def _is_non_public(ip: "ipaddress._BaseAddress") -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def is_public_url(url: str) -> bool:
    """Reject non-http(s) schemes and URLs pointing to non-public addresses.

    Defence against SSRF: search results, page text or a redirect chain could
    point at 127.0.0.1, 169.254.169.254 (cloud metadata), 10.x/192.168.x, or
    file:///etc/passwd. We resolve the hostname and check every A/AAAA record
    against ipaddress.is_private / is_loopback / is_link_local / is_reserved
    before issuing the request.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    # Literal IP in the URL: check directly, don't resolve.
    try:
        return not _is_non_public(ipaddress.ip_address(host))
    except ValueError:
        pass
    # Hostname: resolve all addresses and reject if any is non-public. This
    # is stricter than checking only the first A record: a hostile DNS could
    # return [1.1.1.1, 127.0.0.1] and some clients would try both.
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:
        debug_log(f"DNS lookup failed for {host}: {e}", "web")
        return False
    for info in infos:
        try:
            addr = info[4][0]
            if _is_non_public(ipaddress.ip_address(addr)):
                debug_log(f"Rejecting {url}: resolves to non-public {addr}", "web")
                return False
        except Exception:
            return False
    return True


class NonPublicUrlError(ValueError):
    """The URL, or a redirect it led to, is not a public http(s) address."""


def fetch_public(url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 15.0, stream: bool = False) -> "requests.Response":
    """GET ``url``, re-validating the initial URL and every redirect hop.

    Returns the final (non-redirect) response, open and unconsumed. Raises
    ``NonPublicUrlError`` before any request fires for a non-public URL or
    redirect target, and for a chain longer than ``MAX_REDIRECTS``.
    """
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_public_url(current_url):
            raise NonPublicUrlError("Refusing to fetch a non-public address.")
        response = requests.get(
            current_url, headers=headers, timeout=timeout,
            allow_redirects=False, stream=stream,
        )
        if not (response.is_redirect or response.is_permanent_redirect):
            return response
        location = response.headers.get("Location", "")
        response.close()
        if not location:
            raise NonPublicUrlError("Redirect without a target.")
        current_url = urljoin(current_url, location)
    raise NonPublicUrlError("Too many redirects.")
