"""The web chat's loopback HTTP server: guards, static files and the JSON API, over real sockets.

The daemon is a fake and storage is a temporary file. See ``webchat/webchat.spec.md``.
"""
import http.client
import json
import threading
import time

import pytest

from fake_webchat_backend import FakeWebChatBackend
from jarvis.memory.chat_store import ANY_PROJECT, ChatStore
from jarvis.webchat.hub import ChatHub
from jarvis.webchat.server import ServerSettings, WebChatServer


@pytest.fixture
def parts(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>Jarvis Chat</title><script src='/assets/app.js'></script>")
    (static / "assets" / "app.js").write_text("console.log('hi')")
    (static / "assets" / "app.css").write_text("body{}")
    (tmp_path / "secret.txt").write_text("do not serve")
    backend = FakeWebChatBackend()
    store = ChatStore(str(tmp_path / "jarvis.db"))
    hub = ChatHub(backend, store, sync_interval_sec=0.05)
    server = WebChatServer(hub, ServerSettings(host="127.0.0.1", port=0, static_dir=static, poll_timeout_sec=0.3))
    server.start()
    yield type("Parts", (), {"server": server, "backend": backend, "store": store, "hub": hub})()
    server.stop()
    store.close()


def call(parts, method, path, body=None, headers=None, *, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", parts.server.port, timeout=5)
    sent = {"Host": f"127.0.0.1:{parts.server.port}"}
    payload = raw
    if body is not None:
        payload = json.dumps(body)
        sent["Content-Type"] = "application/json"
    sent.update(headers or {})
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    if payload is not None:
        sent.setdefault("Content-Length", str(len(payload.encode() if isinstance(payload, str) else payload)))
    for name, value in sent.items():
        if value is not None:
            conn.putheader(name, value)
    conn.endheaders(payload.encode() if isinstance(payload, str) else payload)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    try:
        parsed = json.loads(data) if data and response.getheader("Content-Type", "").startswith("application/json") else data
    except ValueError:
        parsed = data
    return response.status, parsed, response


class TestGuards:
    def test_a_foreign_host_is_refused(self, parts):
        status, _, _ = call(parts, "GET", "/api/state", headers={"Host": "evil.example"})
        assert status == 403

    def test_a_foreign_origin_is_refused(self, parts):
        status, _, _ = call(parts, "POST", "/api/stop", body={}, headers={"Origin": "http://evil.example"})
        assert status == 403
        assert parts.backend.cancelled == 0

    def test_a_cross_site_fetch_is_refused_even_without_an_origin(self, parts):
        status, _, _ = call(parts, "POST", "/api/stop", body={}, headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403
        assert parts.backend.cancelled == 0

    def test_the_pages_own_origin_is_served(self, parts):
        origin = f"http://127.0.0.1:{parts.server.port}"
        status, _, _ = call(parts, "POST", "/api/stop", body={}, headers={"Origin": origin})
        assert status == 200

    def test_a_body_must_be_json(self, parts):
        status, _, _ = call(parts, "POST", "/api/chat", raw="text=hi",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert status == 415
        assert parts.backend.submitted == []

    def test_a_huge_body_is_refused(self, parts):
        status, _, _ = call(parts, "POST", "/api/chat", raw="x" * 70_000, headers={"Content-Type": "application/json"})
        assert status == 413

    def test_bad_json_is_refused(self, parts):
        status, body, _ = call(parts, "POST", "/api/chat", raw="{nope", headers={"Content-Type": "application/json"})
        assert status == 400

    def test_responses_carry_the_security_headers(self, parts):
        _, _, response = call(parts, "GET", "/api/state")
        assert "default-src 'self'" in response.getheader("Content-Security-Policy")
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Cache-Control") == "no-store"

    def test_unknown_routes_are_404(self, parts):
        assert call(parts, "GET", "/api/nothing")[0] == 404
        assert call(parts, "GET", "/nothing")[0] == 404


class TestStaticFiles:
    def test_the_page_is_served(self, parts):
        status, body, response = call(parts, "GET", "/")
        assert status == 200 and b"Jarvis Chat" in body
        assert response.getheader("Content-Type").startswith("text/html")

    def test_assets_are_served_with_their_types(self, parts):
        status, _, response = call(parts, "GET", "/assets/app.js")
        assert status == 200 and "javascript" in response.getheader("Content-Type")
        assert call(parts, "GET", "/assets/app.css")[2].getheader("Content-Type").startswith("text/css")

    @pytest.mark.parametrize("path", ["/assets/../../secret.txt", "/assets/..%2f..%2fsecret.txt", "/../secret.txt",
                                      "/assets/%2e%2e/%2e%2e/secret.txt", "/assets/missing.js", "/assets/"])
    def test_nothing_outside_the_assets_folder_is_served(self, parts, path):
        status, body, _ = call(parts, "GET", path)
        assert status == 404 and b"do not serve" not in body


class TestStateAndLibrary:
    def test_state_reports_readiness_mode_and_model(self, parts):
        status, body, _ = call(parts, "GET", "/api/state")
        assert status == 200
        assert body["ready"] is True and body["mode"]["mode"] == "local"
        assert body["model"]["current"] == "gemma4:12b"

    def test_the_model_list_names_installed_models(self, parts):
        status, body, _ = call(parts, "GET", "/api/models")
        assert status == 200
        assert {m["id"] for m in body["models"]} == {"gemma4:12b", "qwen3.5:9b"}
        assert body["mode"]["enabled"] == ["local", "claude"]

    def test_the_library_lists_projects_and_chats(self, parts):
        parts.store.create_project("Work")
        status, body, _ = call(parts, "GET", "/api/library")
        assert status == 200
        assert [p["name"] for p in body["projects"]] == ["Work"]
        assert len(body["chats"]) == 1 and body["active_chat_id"] == body["chats"][0]["id"]


class TestProjects:
    def test_create_rename_delete(self, parts):
        status, project, _ = call(parts, "POST", "/api/projects", {"name": "  Garden "})
        assert status == 201 and project["name"] == "Garden"
        assert call(parts, "PATCH", f"/api/projects/{project['id']}", {"name": "Allotment"})[0] == 200
        assert parts.store.list_projects()[0].name == "Allotment"
        assert call(parts, "DELETE", f"/api/projects/{project['id']}")[0] == 200
        assert parts.store.list_projects() == []

    def test_an_empty_name_is_refused(self, parts):
        assert call(parts, "POST", "/api/projects", {"name": "  "})[0] == 400
        assert call(parts, "POST", "/api/projects", {})[0] == 400

    def test_unknown_projects_are_404(self, parts):
        assert call(parts, "PATCH", "/api/projects/nope", {"name": "x"})[0] == 404
        assert call(parts, "DELETE", "/api/projects/nope")[0] == 404

    def test_deleting_a_project_keeps_its_chats_unfiled(self, parts):
        project = parts.store.create_project("Work")
        chat = parts.store.create_chat(project_id=project.id)
        call(parts, "DELETE", f"/api/projects/{project.id}")
        assert parts.store.get_chat(chat.id).project_id is None


class TestChats:
    def test_creating_a_chat_opens_it(self, parts):
        status, body, _ = call(parts, "POST", "/api/chats", {})
        assert status == 201 and parts.store.get_active_chat_id() == body["chat"]["id"]

    def test_a_chat_can_be_created_in_a_project(self, parts):
        project = parts.store.create_project("Work")
        _, body, _ = call(parts, "POST", "/api/chats", {"project_id": project.id})
        assert body["chat"]["project_id"] == project.id

    def test_creating_in_an_unknown_project_is_404(self, parts):
        assert call(parts, "POST", "/api/chats", {"project_id": "nope"})[0] == 404

    def test_creating_is_409_while_jarvis_is_busy(self, parts):
        parts.backend.busy = True
        status, body, _ = call(parts, "POST", "/api/chats", {})
        assert status == 409 and body["error"] == "busy"

    def test_a_chat_comes_back_with_its_messages(self, parts):
        call(parts, "POST", "/api/chat", {"text": "hello"})
        chat_id = parts.store.get_active_chat_id()
        status, body, _ = call(parts, "GET", f"/api/chats/{chat_id}")
        assert status == 200
        assert [m["text"] for m in body["messages"]] == ["hello", "Done."]
        assert body["chat"]["id"] == chat_id

    def test_rename_and_move(self, parts):
        project = parts.store.create_project("Work")
        chat_id = parts.store.get_active_chat_id() or call(parts, "GET", "/api/library")[1]["active_chat_id"]
        assert call(parts, "PATCH", f"/api/chats/{chat_id}", {"title": "Plans", "project_id": project.id})[0] == 200
        chat = parts.store.get_chat(chat_id)
        assert (chat.title, chat.project_id) == ("Plans", project.id)
        assert call(parts, "PATCH", f"/api/chats/{chat_id}", {"project_id": None})[0] == 200
        assert parts.store.get_chat(chat_id).project_id is None

    def test_moving_into_an_unknown_project_is_404(self, parts):
        chat_id = call(parts, "GET", "/api/library")[1]["active_chat_id"]
        assert call(parts, "PATCH", f"/api/chats/{chat_id}", {"project_id": "nope"})[0] == 404

    def test_a_bad_title_is_refused(self, parts):
        chat_id = call(parts, "GET", "/api/library")[1]["active_chat_id"]
        assert call(parts, "PATCH", f"/api/chats/{chat_id}", {"title": 5})[0] == 400
        assert call(parts, "PATCH", f"/api/chats/{chat_id}", {"title": "  "})[0] == 400

    def test_opening_switches_the_conversation(self, parts):
        call(parts, "POST", "/api/chat", {"text": "about trains"})
        trains = parts.store.get_active_chat_id()
        call(parts, "POST", "/api/chats", {})
        status, body, _ = call(parts, "POST", f"/api/chats/{trains}/open")
        assert status == 200 and body["chat"]["id"] == trains
        assert [m["content"] for m in parts.backend.memory.all_messages()] == ["about trains", "Done."]

    def test_opening_is_409_while_busy_and_404_when_unknown(self, parts):
        chat_id = call(parts, "GET", "/api/library")[1]["active_chat_id"]
        assert call(parts, "POST", "/api/chats/nope/open")[0] == 404
        parts.backend.busy = True
        assert call(parts, "POST", f"/api/chats/{chat_id}/open")[0] == 409

    def test_delete(self, parts):
        _, body, _ = call(parts, "POST", "/api/chats", {})
        other = body["chat"]["id"]
        assert call(parts, "DELETE", f"/api/chats/{other}")[0] == 200
        assert parts.store.get_chat(other) is None
        assert call(parts, "DELETE", f"/api/chats/{other}")[0] == 404

    def test_deleting_everything_needs_confirmation(self, parts):
        parts.store.create_project("Work")
        assert call(parts, "POST", "/api/clear", {})[0] == 400
        assert parts.store.list_projects() != []
        assert call(parts, "POST", "/api/clear", {"confirm": True})[0] == 200
        assert parts.store.list_projects() == [] and parts.store.list_chats(ANY_PROJECT) == []


class TestSending:
    def test_a_message_is_accepted_and_answered(self, parts):
        status, body, _ = call(parts, "POST", "/api/chat", {"text": "hello"})
        assert status == 202 and "query_id" in body
        assert parts.backend.submitted == ["hello"]

    @pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "x" * 4001}])
    def test_bad_text_is_refused(self, parts, body):
        assert call(parts, "POST", "/api/chat", body)[0] == 400
        assert parts.backend.submitted == []

    def test_busy_is_409_and_unready_is_503_with_the_reason_as_the_error(self, parts):
        parts.backend.mode = "busy"
        status, body, _ = call(parts, "POST", "/api/chat", {"text": "hi"})
        assert (status, body["error"]) == (409, "busy")
        parts.backend.mode = "unavailable"
        status, body, _ = call(parts, "POST", "/api/chat", {"text": "hi"})
        assert (status, body["error"]) == (503, "unavailable")

    def test_stop_cancels_the_request(self, parts):
        assert call(parts, "POST", "/api/stop", {})[0] == 200
        assert parts.backend.cancelled == 1


class TestModelSelector:
    def test_a_reply_mode_is_switched(self, parts):
        status, _, _ = call(parts, "POST", "/api/model", {"kind": "mode", "value": "claude"})
        assert status == 200 and parts.backend.reply_mode["mode"] == "claude"

    def test_a_local_model_is_switched(self, parts):
        status, _, _ = call(parts, "POST", "/api/model", {"kind": "local", "value": "qwen3.5:9b"})
        assert status == 200 and parts.backend.local_model["current"] == "qwen3.5:9b"

    def test_a_refusal_is_409_with_the_reason(self, parts):
        parts.backend.refuse_mode = "not_enabled"
        status, body, _ = call(parts, "POST", "/api/model", {"kind": "mode", "value": "codex"})
        assert status == 409 and body["error"] == "not_enabled"

    def test_a_cloud_model_and_effort_are_switched(self, parts):
        status, _, _ = call(parts, "POST", "/api/model", {"kind": "cloud", "value": "sonnet", "effort": "high"})
        assert status == 200 and parts.backend.cloud_calls == [("sonnet", "high")]

    def test_a_cloud_model_without_an_effort_is_allowed(self, parts):
        assert call(parts, "POST", "/api/model", {"kind": "cloud", "value": "haiku"})[0] == 200
        assert parts.backend.cloud_calls == [("haiku", None)]

    def test_a_cloud_refusal_is_409_with_the_reason(self, parts):
        parts.backend.refuse_cloud = "effort_unsupported"
        status, body, _ = call(parts, "POST", "/api/model", {"kind": "cloud", "value": "sonnet", "effort": "max"})
        assert status == 409 and body["error"] == "effort_unsupported"

    def test_the_model_list_includes_the_cloud_models(self, parts):
        parts.backend.cloud = {"mode": "claude", "model": "sonnet", "effort": "low", "ready": True}
        parts.backend.cloud_options = [{"id": "sonnet", "name": "Sonnet", "is_default": False, "efforts": []}]
        body = call(parts, "GET", "/api/models")[1]
        assert body["cloud"]["mode"] == "claude" and body["cloud"]["models"][0]["id"] == "sonnet"

    def test_the_state_includes_the_cloud_choice(self, parts):
        parts.backend.cloud = {"mode": "claude", "model": "sonnet", "effort": "low", "ready": True}
        assert call(parts, "GET", "/api/state")[1]["cloud"]["effort"] == "low"

    @pytest.mark.parametrize("body", [{}, {"kind": "mode"}, {"kind": "other", "value": "x"}, {"kind": "local", "value": 3},
                                      {"kind": "cloud", "value": "x", "effort": 5}, {"kind": "cloud", "value": 3}])
    def test_bad_requests_are_refused(self, parts, body):
        assert call(parts, "POST", "/api/model", body)[0] == 400


class TestPolling:
    def test_a_poll_returns_the_snapshot(self, parts):
        status, body, _ = call(parts, "GET", "/api/poll?rev=-1&after=0")
        assert status == 200 and body["ready"] is True and "rev" in body

    def test_a_poll_waits_for_news_and_then_returns_it(self, parts):
        _, first, _ = call(parts, "GET", "/api/poll?rev=-1&after=0")
        threading.Timer(0.05, lambda: parts.backend.speak("hi", "hello")).start()
        deadline = time.time() + 5
        body = first
        while time.time() < deadline and not body["messages"]:
            _, body, _ = call(parts, "GET", f"/api/poll?rev={body['rev']}&after=0")
        assert [m["text"] for m in body["messages"]] == ["hi", "hello"]

    def test_non_numeric_parameters_are_ignored(self, parts):
        assert call(parts, "GET", "/api/poll?rev=abc&after=xyz")[0] == 200
