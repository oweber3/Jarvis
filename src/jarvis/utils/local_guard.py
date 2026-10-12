"""Host, Origin and fetch-site checks for the web pages Jarvis serves without a login.

Other web pages must not be able to reach such a server: a rebound DNS name arrives with a foreign
``Host``, and a cross-site request carries a foreign ``Origin`` or ``Sec-Fetch-Site: cross-site``. Requests
with no ``Origin`` (the page's own GETs, other local clients) are served.
"""
from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def host_name(netloc: str) -> str:
    """Lower-case hostname of a ``host[:port]`` string, without brackets or port."""
    try:
        return (urlsplit(f"//{netloc}").hostname or "").lower()
    except ValueError:
        return ""


def is_loopback_host(name: str) -> bool:
    return name in LOOPBACK_HOSTS


def refusal(host_header: str, origin_header: Optional[str], sec_fetch_site: Optional[str] = None, *,
            host_allowed: Callable[[str], bool] = is_loopback_host) -> Optional[str]:
    """Why a request must be refused: ``"host"``, ``"origin"``, or ``None`` when it may be served.

    ``host_allowed`` receives the lower-case hostname of the ``Host`` header; by default only this PC's
    loopback names pass. Pass ``sec_fetch_site`` only for requests a cross-site page must never make.
    """
    host = host_header or ""
    if not host_allowed(host_name(host)):
        return "host"
    if origin_header is not None:
        try:
            origin_netloc = urlsplit(origin_header).netloc.lower()
        except ValueError:
            origin_netloc = ""
        if origin_netloc != host.lower():
            return "origin"
    if sec_fetch_site == "cross-site":
        return "origin"
    return None
