"""The Host and Origin checks shared by the local web pages (Memory Viewer and web chat).

A page served on this PC with no login must only answer itself: a rebound DNS name arrives with a
foreign Host, and a request from another site carries a foreign Origin.
"""
import pytest

from jarvis.utils.local_guard import refusal


@pytest.mark.parametrize("host", ["localhost", "localhost:8766", "127.0.0.1:5050", "[::1]:5050", "LOCALHOST:80"])
def test_loopback_hosts_are_served(host):
    assert refusal(host, None) is None


@pytest.mark.parametrize("host", ["", "evil.example", "evil.example:8766", "192.168.1.5:8766", "localhost.evil.example",
                                  "127.0.0.1.evil.example", "0.0.0.0:8766"])
def test_other_hosts_are_refused(host):
    assert refusal(host, None) == "host"


def test_a_request_from_the_page_itself_is_served():
    assert refusal("127.0.0.1:8766", "http://127.0.0.1:8766") is None


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:9999", "http://localhost:8766", "null"])
def test_a_request_from_another_origin_is_refused(origin):
    assert refusal("127.0.0.1:8766", origin) == "origin"
