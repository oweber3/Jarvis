"""Host and Origin checks for the web pages Jarvis serves on this PC without a login.

Other web pages must not be able to reach such a server: a rebound DNS name arrives with a foreign
``Host``, and a cross-site request carries a foreign ``Origin``. Requests with no ``Origin`` (the page's
own GETs, other local clients) are served.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def host_name(netloc: str) -> str:
    """Lower-case hostname of a ``host[:port]`` string, without brackets or port."""
    try:
        return (urlsplit(f"//{netloc}").hostname or "").lower()
    except ValueError:
        return ""


def refusal(host_header: str, origin_header: Optional[str]) -> Optional[str]:
    """Why a request must be refused: ``"host"``, ``"origin"``, or ``None`` when it may be served."""
    host = host_header or ""
    if host_name(host) not in LOOPBACK_HOSTS:
        return "host"
    if origin_header is not None:
        try:
            origin_netloc = urlsplit(origin_header).netloc.lower()
        except ValueError:
            origin_netloc = ""
        if origin_netloc != host.lower():
            return "origin"
    return None
