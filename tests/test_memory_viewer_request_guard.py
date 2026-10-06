"""The memory viewer answers only requests that come from the viewer itself.

Other web pages (cross-site requests, DNS rebinding) must not be able to read or change
the diary through the loopback server.
"""

from __future__ import annotations

import pytest

try:
    import flask  # noqa: F401

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False

from src.jarvis.memory.graph import GraphMemoryStore


@pytest.mark.unit
@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestMemoryViewerRequestGuard:

    @pytest.fixture(autouse=True)
    def setup_app(self, tmp_path):
        from src.desktop_app import memory_viewer

        self.store = GraphMemoryStore(str(tmp_path / "test.db"))
        memory_viewer._graph_store = self.store
        memory_viewer.app.config["TESTING"] = True
        self.client = memory_viewer.app.test_client()

    # -- Host ---------------------------------------------------------------

    @pytest.mark.parametrize("host", [
        "localhost", "localhost:5050", "127.0.0.1:5050", "[::1]:5050", "LOCALHOST:5050",
    ])
    def test_loopback_hosts_are_served(self, host):
        response = self.client.get("/api/graph/presets", headers={"Host": host})
        assert response.status_code == 200

    @pytest.mark.parametrize("host", [
        "evil.example", "evil.example:5050", "127.0.0.1.evil.example:5050", "192.168.1.20:5050", "",
    ])
    def test_other_hosts_are_refused(self, host):
        """A rebound DNS name still arrives with the attacker's name in Host."""
        response = self.client.get("/api/graph/presets", headers={"Host": host})
        assert response.status_code == 403
        assert b"ids" not in response.data

    # -- Origin -------------------------------------------------------------

    def test_requests_from_the_viewer_page_itself_are_served(self):
        response = self.client.get(
            "/api/graph/presets",
            headers={"Host": "localhost:5050", "Origin": "http://localhost:5050"})
        assert response.status_code == 200

    def test_requests_without_an_origin_are_served(self):
        """Same-origin GETs and non-browser clients send no Origin."""
        assert self.client.get("/api/graph/presets").status_code == 200

    @pytest.mark.parametrize("origin", [
        "https://evil.example",
        "null",
        "http://localhost:8000",     # another local server on a different port
        "http://127.0.0.1:5050",     # same machine, different origin string than Host
    ])
    def test_foreign_origins_are_refused(self, origin):
        response = self.client.get(
            "/api/graph/presets", headers={"Host": "localhost:5050", "Origin": origin})
        assert response.status_code == 403

    def test_cross_site_delete_changes_nothing(self):
        node = self.store.create_node(name="Private", description="d", parent_id="root")

        response = self.client.delete(
            f"/api/graph/node/{node.id}",
            headers={"Host": "localhost:5050", "Origin": "https://evil.example"})

        assert response.status_code == 403
        assert self.store.get_node(node.id) is not None

    def test_cross_site_post_that_would_rewrite_the_diary_is_refused(self):
        response = self.client.post(
            "/api/diary/scrub-deflections",
            headers={"Host": "localhost:5050", "Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_a_malformed_origin_is_refused_not_an_error(self):
        response = self.client.get(
            "/api/graph/presets", headers={"Host": "localhost:5050", "Origin": "http://[bad"})
        assert response.status_code == 403
