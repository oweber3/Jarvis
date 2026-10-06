"""Tests for fetch web page tool."""

import pytest
from unittest.mock import Mock, patch
import requests

from src.jarvis.tools.builtin.fetch_web_page import FetchWebPageTool
from src.jarvis.tools.base import ToolContext
from src.jarvis.tools.types import ToolExecutionResult


def _make_response_mock(**attrs) -> Mock:
    """Build a Mock that doubles as both the requests response and a context
    manager (the production code uses ``with requests.get(...) as resp`` so
    the connection is released deterministically).
    """
    attrs.setdefault("is_redirect", False)
    attrs.setdefault("is_permanent_redirect", False)
    resp = Mock(**attrs)
    resp.__enter__ = Mock(return_value=resp)
    resp.__exit__ = Mock(return_value=False)
    return resp


@pytest.fixture(autouse=True)
def _public_dns_for_named_hosts():
    """Named hosts resolve to a public address so no test needs a network."""
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
        yield


class TestFetchWebPageTool:
    """Test fetch web page tool functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tool = FetchWebPageTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    def test_tool_properties(self):
        """Test tool metadata properties."""
        assert self.tool.name == "fetchWebPage"
        assert "fetch" in self.tool.description.lower()
        assert self.tool.inputSchema["type"] == "object"
        assert "url" in self.tool.inputSchema["required"]

    def test_run_no_args(self):
        """Test fetch web page with no arguments."""
        result = self.tool.run(None, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "url" in result.reply_text.lower()

    def test_run_empty_url(self):
        """Test fetch web page with empty URL."""
        args = {"url": ""}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "url" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_success(self, mock_get):
        """Test successful web page fetch."""
        mock_response = _make_response_mock(
            status_code=200,
            text='<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            content=b'<html><head><title>Test</title></head><body><p>Content</p></body></html>',
            headers={'content-type': 'text/html'},
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        args = {"url": "https://example.com"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "example.com" in result.reply_text
        self.context.user_print.assert_called()

    @patch('requests.get')
    def test_run_success_without_beautifulsoup(self, mock_get):
        """Test successful web page fetch without BeautifulSoup."""
        mock_response = _make_response_mock(
            status_code=200,
            text='<html><body>Raw content</body></html>',
            content=b'<html><body>Raw content</body></html>',
            headers={'content-type': 'text/html'},
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        with patch('builtins.__import__', side_effect=ImportError):
            args = {"url": "https://example.com"}
            result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is True
        assert "Raw Content" in result.reply_text

    @patch('requests.get')
    def test_run_http_error(self, mock_get):
        """Test fetch web page with HTTP error."""
        mock_response = _make_response_mock(status_code=404)
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_get.return_value = mock_response

        args = {"url": "https://example.com/notfound"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "Failed to fetch page" in result.reply_text

    @patch('requests.get')
    def test_run_request_error(self, mock_get):
        """Test fetch web page with network error."""
        mock_get.side_effect = requests.exceptions.RequestException("Network error")

        args = {"url": "https://example.com"}
        result = self.tool.run(args, self.context)

        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "Failed to fetch page" in result.reply_text

    def test_run_invalid_url(self):
        """Test fetch web page with invalid URL."""
        args = {"url": "not-a-url"}
        result = self.tool.run(args, self.context)
        assert isinstance(result, ToolExecutionResult)
        assert result.success is False
        assert "failed" in result.reply_text.lower() or "error" in result.reply_text.lower()

    @patch('requests.get')
    def test_run_with_links_extraction(self, mock_get):
        """Test fetch web page including link extraction when include_links=True."""
        html = (
            '<html><head><title>Links Page</title></head>'
            '<body><p>Intro</p>'
            '<a href="/relative">Relative Link</a>'
            '<a href="https://absolute.test/page">Absolute Link</a>'
            '<a href="mailto:test@example.com">Mail</a>'
            '</body></html>'
        )
        mock_response = _make_response_mock(
            status_code=200,
            text=html,
            content=html.encode(),
            raise_for_status=Mock(),
        )
        mock_get.return_value = mock_response

        args = {"url": "https://example.com", "include_links": True}
        result = self.tool.run(args, self.context)
        assert result.success is True
        assert isinstance(result, ToolExecutionResult)
        assert "Links found on page" in result.reply_text
        # relative link should be resolved to absolute
        assert "https://example.com/relative" in result.reply_text
        assert "absolute.test" in result.reply_text


def _public_dns():
    """Resolve every hostname to a public address so tests never need a network."""
    return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])


class TestFetchWebPageRefusesNonPublicAddresses:
    """fetchWebPage must never reach loopback, LAN or link-local services."""

    def setup_method(self):
        self.tool = FetchWebPageTool()
        self.context = Mock(spec=ToolContext)
        self.context.user_print = Mock()

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:5050/api/memories?limit=500",
        "http://192.168.1.50:8060/query/apps",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
    ])
    def test_non_public_literal_addresses_are_refused_without_a_request(self, url):
        with patch("requests.get") as mock_get:
            result = self.tool.run({"url": url}, self.context)
        assert result.success is False
        mock_get.assert_not_called()

    def test_hostname_resolving_to_loopback_is_refused(self):
        loopback = [(2, 1, 6, "", ("127.0.0.1", 0))]
        with patch("socket.getaddrinfo", return_value=loopback), patch("requests.get") as mock_get:
            result = self.tool.run({"url": "https://rebind.example"}, self.context)
        assert result.success is False
        mock_get.assert_not_called()

    def test_hostname_with_any_private_address_is_refused(self):
        mixed = [(2, 1, 6, "", ("1.1.1.1", 0)), (2, 1, 6, "", ("192.168.0.9", 0))]
        with patch("socket.getaddrinfo", return_value=mixed), patch("requests.get") as mock_get:
            result = self.tool.run({"url": "https://mixed.example"}, self.context)
        assert result.success is False
        mock_get.assert_not_called()

    def test_localhost_name_is_refused(self):
        loopback = [(2, 1, 6, "", ("127.0.0.1", 0)), (23, 1, 6, "", ("::1", 0, 0, 0))]
        with patch("socket.getaddrinfo", return_value=loopback), patch("requests.get") as mock_get:
            result = self.tool.run({"url": "http://localhost:5050/api/memories"}, self.context)
        assert result.success is False
        mock_get.assert_not_called()

    def test_redirect_from_public_page_to_loopback_is_refused(self):
        redirect = _make_response_mock(
            status_code=302, is_redirect=True, is_permanent_redirect=False,
            headers={"Location": "http://127.0.0.1:5050/api/memories"},
        )
        redirect.close = Mock()
        with _public_dns(), patch("requests.get", return_value=redirect) as mock_get:
            result = self.tool.run({"url": "https://example.com/go"}, self.context)
        assert result.success is False
        assert mock_get.call_count == 1  # the loopback hop was never requested
        assert all(c.kwargs.get("allow_redirects") is False for c in mock_get.call_args_list)

    def test_public_redirect_is_followed_and_each_hop_checked(self):
        redirect = _make_response_mock(
            status_code=301, is_redirect=True, is_permanent_redirect=False,
            headers={"Location": "/final"},
        )
        redirect.close = Mock()
        html = "<html><head><title>Final</title></head><body><p>Landed here</p></body></html>"
        final = _make_response_mock(
            status_code=200, is_redirect=False, is_permanent_redirect=False,
            text=html, content=html.encode(), raise_for_status=Mock(),
        )
        with _public_dns() as dns, patch("requests.get", side_effect=[redirect, final]) as mock_get:
            result = self.tool.run({"url": "https://example.com/start"}, self.context)
        assert result.success is True
        assert "Landed here" in result.reply_text
        assert mock_get.call_count == 2
        assert dns.call_count >= 2  # initial URL and the redirect target were both resolved

    def test_endless_redirects_are_cut_off(self):
        redirect = _make_response_mock(
            status_code=302, is_redirect=True, is_permanent_redirect=False,
            headers={"Location": "https://example.com/again"},
        )
        redirect.close = Mock()
        with _public_dns(), patch("requests.get", return_value=redirect) as mock_get:
            result = self.tool.run({"url": "https://example.com/loop"}, self.context)
        assert result.success is False
        assert mock_get.call_count <= 10

    def test_real_loopback_server_content_never_reaches_the_result(self):
        import http.server
        import threading

        class _Diary(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><body><p>DIARY: private matter</p></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Diary)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = self.tool.run(
                {"url": f"http://127.0.0.1:{server.server_port}/api/memories"}, self.context)
        finally:
            server.shutdown()
            server.server_close()
        assert "private matter" not in (result.reply_text or "")
        assert result.success is False


class TestUrlGuard:
    def test_non_http_schemes_are_not_public(self):
        from src.jarvis.tools.builtin.url_guard import is_public_url
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/x", "javascript:alert(1)"):
            assert is_public_url(url) is False
