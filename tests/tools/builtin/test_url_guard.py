"""The SSRF guard behind fetchWebPage and webSearch's page fetch, driven over real loopback HTTP.

Nothing leaves this PC. Names resolve through a fake DNS table, numeric addresses are never looked up on
the network, and two loopback servers share one port: 127.0.0.1 plays a private service on this PC and
127.0.0.2 plays a public website (the address policy is told to count 127.0.0.2 as public).
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest
import requests

from jarvis.tools.base import ToolContext
from jarvis.tools.builtin import url_guard, web_search
from jarvis.tools.builtin.fetch_web_page import FetchWebPageTool
from jarvis.tools.builtin.url_guard import NonPublicUrlError, fetch_public, is_public_address, is_public_url

PRIVATE_HOST = "127.0.0.1"
PUBLIC_STAND_IN = "127.0.0.2"
_REAL_GETADDRINFO = socket.getaddrinfo


class _QuietServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer.server_bind looks up the host name (reverse DNS); these tests stay off the network.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Site:
    """A tiny web server on one loopback address that records what it was asked."""

    def __init__(self, host: str, port: int, routes: dict, ssl_context: ssl.SSLContext | None = None) -> None:
        self.requests: list[tuple[str, str | None]] = []  # (path, Host header)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Host")))
                status, headers, body = routes.get(self.path, (404, {}, b"not here"))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = _QuietServer((host, port), Handler)
        if ssl_context is not None:
            self._server.socket = ssl_context.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def paths(self) -> list[str]:
        return [path for path, _host in self.requests]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _page(text: str) -> tuple:
    return 200, {"Content-Type": "text/html"}, f"<html><body><p>{text}</p></body></html>".encode()


def _redirect(location: str) -> tuple:
    return 302, {"Location": location}, b""


class _Dns:
    """Name to answers; a list of answers is served one per lookup (the last one repeats)."""

    def __init__(self) -> None:
        self.table: dict[str, list[str]] = {}
        self.lookups: dict[str, int] = {}

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        key = str(host).lower()
        if key in self.table:
            count = self.lookups.get(key, 0)
            self.lookups[key] = count + 1
            answers = self.table[key]
            ip = answers[min(count, len(answers) - 1)]
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(fam, socket.SOCK_STREAM, 6, "", (ip, port or 0))]
        # Numeric addresses only: AI_NUMERICHOST guarantees no query leaves this PC.
        return _REAL_GETADDRINFO(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)


@pytest.fixture
def dns(monkeypatch):
    fake = _Dns()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return fake


@pytest.fixture
def stand_in_is_public(monkeypatch):
    real = url_guard.is_public_address
    monkeypatch.setattr(url_guard, "is_public_address",
                        lambda ip: str(ip) == PUBLIC_STAND_IN or real(ip))


@pytest.fixture
def sites(dns, stand_in_is_public):
    """(private service on 127.0.0.1, public website on 127.0.0.2), both on the same port."""
    private = _Site(PRIVATE_HOST, 0, {"/secret": _page("DIARY private matter"),
                                      "/": _page("DIARY private matter")})
    routes: dict = {}
    public = _Site(PUBLIC_STAND_IN, private.port, routes)
    public.routes = routes
    yield private, public
    public.close()
    private.close()


def _context():
    context = Mock(spec=ToolContext)
    context.user_print = Mock()
    return context


# --- one parse: a URL means one host to every parser ------------------------------------------------

@pytest.mark.parametrize("template", [
    "http://127.0.0.1:{port}\\@example.com/secret",
    "http://127.0.0.1:{port}\\@example.com/",
    "http://example.com\\@127.0.0.1:{port}/secret",
])
def test_a_backslash_in_the_authority_is_refused_before_any_request(sites, dns, template):
    private, _public = sites
    dns.table["example.com"] = ["93.184.216.34"]  # never contacted: the URL is refused first
    url = template.format(port=private.port)

    assert is_public_url(url) is False
    with pytest.raises(NonPublicUrlError):
        fetch_public(url, timeout=3)
    assert web_search._fetch_page_content(url) is None
    result = FetchWebPageTool().run({"url": url}, _context())

    assert result.success is False
    assert private.requests == []


# --- one lookup: the connection goes to the address that was checked --------------------------------

def test_a_changed_dns_answer_cannot_move_the_connection_to_loopback(sites, dns):
    private, public = sites
    public.routes["/admin"] = _page("public page")
    dns.table["rebind.invalid"] = [PUBLIC_STAND_IN, PRIVATE_HOST]

    with fetch_public(f"http://rebind.invalid:{private.port}/admin", timeout=3) as response:
        body = response.text

    assert private.requests == []
    assert "public page" in body
    assert public.requests == [("/admin", f"rebind.invalid:{private.port}")]


def test_webpage_tools_follow_the_checked_address_too(sites, dns):
    private, public = sites
    public.routes["/secret"] = _page("public topic page")
    dns.table["rebind.invalid"] = [PUBLIC_STAND_IN, PRIVATE_HOST]
    url = f"http://rebind.invalid:{private.port}/secret"

    content = web_search._fetch_page_content(url)
    dns.lookups.clear()
    result = FetchWebPageTool().run({"url": url}, _context())

    assert private.requests == []
    assert "public topic page" in content
    assert result.success is True and "public topic page" in result.reply_text


def test_a_name_with_any_non_public_address_is_refused(sites, dns):
    private, _public = sites
    dns.table["mixed.invalid"] = [PRIVATE_HOST]
    assert is_public_url(f"http://mixed.invalid:{private.port}/secret") is False
    with pytest.raises(NonPublicUrlError):
        fetch_public(f"http://mixed.invalid:{private.port}/secret", timeout=3)
    assert private.requests == []


# --- redirects: every hop is checked and pinned ------------------------------------------------------

@pytest.mark.parametrize("location", [
    "http://127.0.0.1:{port}/secret",
    "http://127.0.0.1:{port}\\@example.com/secret",
    "http://localhost:{port}/secret",
])
def test_a_redirect_to_a_private_address_is_refused(sites, dns, location):
    private, public = sites
    dns.table["example.com"] = ["93.184.216.34"]  # never contacted: the URL is refused first
    dns.table["localhost"] = [PRIVATE_HOST]
    dns.table["start.invalid"] = [PUBLIC_STAND_IN]
    public.routes["/go"] = _redirect(location.format(port=private.port))
    url = f"http://start.invalid:{private.port}/go"

    with pytest.raises(NonPublicUrlError):
        fetch_public(url, timeout=3)
    assert web_search._fetch_page_content(url) is None
    assert FetchWebPageTool().run({"url": url}, _context()).success is False
    assert private.requests == []


def test_a_redirect_hop_is_pinned_to_its_own_checked_address(sites, dns):
    private, public = sites
    dns.table["start.invalid"] = [PUBLIC_STAND_IN]
    dns.table["next.invalid"] = [PUBLIC_STAND_IN, PRIVATE_HOST]
    public.routes["/go"] = _redirect(f"http://next.invalid:{private.port}/final")
    public.routes["/final"] = _page("landed on the public page")

    with fetch_public(f"http://start.invalid:{private.port}/go", timeout=3) as response:
        body = response.text

    assert "landed on the public page" in body
    assert private.requests == []
    assert public.requests[-1] == ("/final", f"next.invalid:{private.port}")


def test_public_redirects_stop_after_the_limit(sites, dns):
    private, public = sites
    dns.table["loop.invalid"] = [PUBLIC_STAND_IN]
    public.routes["/loop"] = _redirect("/loop")

    with pytest.raises(NonPublicUrlError):
        fetch_public(f"http://loop.invalid:{private.port}/loop", timeout=3)
    assert len(public.requests) == url_guard.MAX_REDIRECTS + 1


# --- TLS: the original host name is sent and verified ------------------------------------------------

def _openssl() -> str | None:
    bundled = os.path.join(sys.prefix, "Library", "bin", "openssl.exe")
    return bundled if os.path.exists(bundled) else shutil.which("openssl")


_CERT_CONFIG = """[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = pinned.invalid
[v3]
subjectAltName = DNS:pinned.invalid
basicConstraints = critical, CA:TRUE
keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign
extendedKeyUsage = serverAuth
"""


@pytest.fixture
def tls_site(tmp_path, dns, stand_in_is_public, monkeypatch):
    openssl = _openssl()
    if openssl is None:
        pytest.skip("openssl is needed to make a test certificate")
    cert, key, config = tmp_path / "cert.pem", tmp_path / "key.pem", tmp_path / "openssl.cnf"
    config.write_text(_CERT_CONFIG, encoding="ascii")
    made = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "1", "-config", str(config)],
        capture_output=True, timeout=60, env={**os.environ, "OPENSSL_CONF": str(config)})
    if made.returncode != 0:
        pytest.skip("openssl could not make a test certificate")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert), str(key))
    names: list = []
    server_context.sni_callback = lambda sock, name, ctx: names.append(name)
    site = _Site(PUBLIC_STAND_IN, 0, {"/": _page("secure page")}, ssl_context=server_context)
    # requests verifies with its preloaded context; make it trust the test certificate only.
    import requests.adapters
    monkeypatch.setattr(requests.adapters, "_preloaded_ssl_context", ssl.create_default_context(cafile=str(cert)))
    dns.table["pinned.invalid"] = [PUBLIC_STAND_IN]
    dns.table["other.invalid"] = [PUBLIC_STAND_IN]
    yield site, names
    site.close()


def test_https_sends_and_verifies_the_original_host_name(tls_site):
    site, names = tls_site

    with fetch_public(f"https://pinned.invalid:{site.port}/", timeout=5) as response:
        body = response.text

    assert "secure page" in body
    assert names == ["pinned.invalid"]
    assert site.requests == [("/", f"pinned.invalid:{site.port}")]


def test_https_refuses_a_certificate_for_another_name(tls_site):
    site, names = tls_site

    with pytest.raises(requests.exceptions.SSLError):
        fetch_public(f"https://other.invalid:{site.port}/", timeout=5)
    assert names == ["other.invalid"]
    assert site.requests == []


# --- address policy ----------------------------------------------------------------------------------

@pytest.mark.parametrize("address", [
    "100.64.0.1", "100.100.100.100", "100.127.255.254",  # shared address space (Tailscale, CGNAT)
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "224.0.0.1",
    "::1", "fe80::1", "fc00::1", "fd7a:115c:a1e0::1", "fec0::1", "ff0e::1",
    "::ffff:127.0.0.1", "::ffff:192.168.1.1", "::ffff:100.100.100.100",  # IPv4-mapped
    "::127.0.0.1", "::8.8.8.8",  # IPv4-compatible
    "64:ff9b::7f00:1", "64:ff9b::c0a8:101", "64:ff9b::6464:6464",  # NAT64
    "2002:7f00:1::1", "2002:c0a8:101::1", "2002:6464:6464::1",  # 6to4
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
])
def test_non_public_addresses_and_embedded_forms_are_refused(address):
    assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "64:ff9b::808:808"])
def test_global_addresses_are_public(address):
    assert is_public_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("url", ["http://100.100.100.100/", "http://100.101.102.103:8123/api/states",
                                 "http://[::ffff:100.64.0.1]/", "http://[fec0::1]/"])
def test_fetch_web_page_sends_nothing_to_shared_or_site_local_addresses(url, monkeypatch, dns):
    sent = Mock(side_effect=AssertionError("a request was sent"))
    monkeypatch.setattr(requests.Session, "get", sent)
    result = FetchWebPageTool().run({"url": url}, _context())
    assert result.success is False
    assert not sent.called


def test_a_name_resolving_into_the_shared_address_space_is_refused(dns):
    dns.table["nas.invalid"] = ["100.101.102.103"]
    assert is_public_url("http://nas.invalid/") is False


# --- body cap ----------------------------------------------------------------------------------------

def test_fetch_web_page_stops_reading_at_the_cap(sites, dns):
    private, public = sites
    padding = " " * (url_guard.MAX_FETCH_BYTES + 1024)
    public.routes["/big"] = (200, {"Content-Type": "text/html"},
                             f"<html><body><p>Early paragraph</p>{padding}<p>Late paragraph</p></body></html>".encode())
    dns.table["big.invalid"] = [PUBLIC_STAND_IN]

    result = FetchWebPageTool().run({"url": f"http://big.invalid:{private.port}/big"}, _context())

    assert result.success is True
    assert "Early paragraph" in result.reply_text
    assert "Late paragraph" not in result.reply_text
