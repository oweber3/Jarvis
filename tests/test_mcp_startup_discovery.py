"""MCP discovery at start-up runs in the background; tool readers wait for it, a peek does not."""
import threading
import time

import pytest

import jarvis.tools.registry as registry


def _slow_client(release: threading.Event):
    class SlowClient:
        def __init__(self, config):
            self.config = config

        def list_tools(self, server_name):
            release.wait(5)
            return [{"name": "fetch", "description": "Fetch a page"}]

    return SlowClient


@pytest.fixture
def fresh_cache(monkeypatch):
    monkeypatch.setattr(registry, "_mcp_tools_cache", {})
    monkeypatch.setattr(registry, "_mcp_config_cache", {})
    yield
    registry._mcp_discovery_done.set()


@pytest.mark.unit
def test_start_up_goes_on_while_mcp_servers_list_their_tools(monkeypatch, fresh_cache):
    release = threading.Event()
    monkeypatch.setattr(registry, "MCPClient", _slow_client(release))
    reported = []

    started = time.monotonic()
    thread = registry.start_mcp_discovery({"web": {"command": "fake"}},
                                          on_done=lambda tools, errors: reported.append((tools, errors)))
    assert time.monotonic() - started < 1.0
    assert reported == []

    release.set()
    thread.join(5)
    tools, errors = reported[0]
    assert "web__fetch" in tools
    assert errors == {}


@pytest.mark.unit
def test_a_request_that_needs_tools_waits_for_discovery_to_finish(monkeypatch, fresh_cache):
    release = threading.Event()
    monkeypatch.setattr(registry, "MCPClient", _slow_client(release))
    thread = registry.start_mcp_discovery({"web": {"command": "fake"}})

    seen = []
    reader = threading.Thread(target=lambda: seen.append(registry.get_cached_mcp_tools()))
    reader.start()
    reader.join(0.3)
    assert reader.is_alive(), "the reader returned before discovery finished"

    release.set()
    reader.join(5)
    thread.join(5)
    assert "web__fetch" in seen[0]


@pytest.mark.unit
def test_a_peek_at_the_tools_does_not_wait_for_discovery(monkeypatch, fresh_cache):
    release = threading.Event()
    monkeypatch.setattr(registry, "MCPClient", _slow_client(release))
    thread = registry.start_mcp_discovery({"web": {"command": "fake"}})
    try:
        started = time.monotonic()
        assert registry.get_cached_mcp_tools(wait=False) == {}
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        thread.join(5)


@pytest.mark.unit
def test_a_failed_discovery_reports_the_error_and_releases_readers(monkeypatch, fresh_cache):
    class BrokenClient:
        def __init__(self, config):
            raise RuntimeError("server missing")

    monkeypatch.setattr(registry, "MCPClient", BrokenClient)
    reported = []
    thread = registry.start_mcp_discovery({"web": {"command": "fake"}},
                                          on_done=lambda tools, errors: reported.append((tools, errors)))
    thread.join(5)

    tools, errors = reported[0]
    assert tools == {}
    assert errors
    started = time.monotonic()
    assert registry.get_cached_mcp_tools() == {}
    assert time.monotonic() - started < 0.5
