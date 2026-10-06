"""Phone access against the real daemon text path: submit_text_query, the shared lock and dialogue memory.

The reply engine is replaced by a stand-in that records turns like the real one, so nothing reaches a model.
"""
import http.client
import json
import threading
import time

import pytest

from jarvis import daemon
from jarvis.memory.conversation import DialogueMemory
from jarvis.remote.backend import DaemonBackend
from jarvis.remote.hub import RemoteHub
from jarvis.remote.pairing import DeviceStore
from jarvis.remote.server import RemoteServer, ServerSettings


@pytest.fixture
def booted(monkeypatch):
    memory = DialogueMemory()
    monkeypatch.setattr(daemon, "_global_dialogue_memory", memory)
    monkeypatch.setattr(daemon, "_global_cfg", object())
    monkeypatch.setattr(daemon, "_global_db", object())
    monkeypatch.setattr(daemon, "_global_stop_requested", False)
    release = threading.Event()
    release.set()

    def fake_engine(db, cfg, tts, text, dialogue_memory, language=None, quiet=False, **_):
        assert tts is None and quiet is True
        release.wait(5)
        from jarvis.utils.redact import redact
        dialogue_memory.add_message("user", redact(text))
        dialogue_memory.add_message("assistant", f"Echo: {text}")
        return f"Echo: {text}"

    monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", fake_engine)
    return memory, release


@pytest.fixture
def server(booted, tmp_path):
    store = DeviceStore(tmp_path)
    server = RemoteServer(RemoteHub(DaemonBackend(), sync_interval_sec=0.05), store,
                          ServerSettings(host="127.0.0.1", port=0, poll_timeout_sec=2.0))
    server.start()
    code = store.start_pairing()
    token = call(server, "POST", "/api/pair", {"code": code, "name": "phone"})[1]["token"]
    server.token = token
    yield server
    server.stop()


def call(server, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token or getattr(server, "token", None):
        headers["Authorization"] = f"Bearer {token or server.token}"
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else None


def wait_for_query(server, query_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, snap = call(server, "GET", "/api/poll?after=0&rev=-1")
        query = snap["queries"].get(str(query_id))
        if query and query["status"] != "pending":
            return snap
        time.sleep(0.05)
    raise AssertionError("query did not finish")


@pytest.mark.integration
class TestPhoneThroughTheDaemon:
    def test_a_phone_request_is_answered_and_joins_the_conversation(self, server, booted):
        memory, _ = booted
        status, body = call(server, "POST", "/api/chat", {"text": "volume up"})
        assert status == 202
        snap = wait_for_query(server, body["query_id"])
        assert snap["queries"][str(body["query_id"])] == {"status": "done", "reply": "Echo: volume up"}
        assert [e["text"] for e in snap["entries"]] == ["volume up", "Echo: volume up"]
        assert [m["content"] for m in memory.all_messages()] == ["volume up", "Echo: volume up"]

    def test_a_second_request_while_one_runs_is_busy(self, server, booted):
        _, release = booted
        release.clear()
        first = call(server, "POST", "/api/chat", {"text": "slow one"})
        assert first[0] == 202
        deadline = time.time() + 2
        while not daemon.is_query_running() and time.time() < deadline:
            time.sleep(0.01)
        assert call(server, "POST", "/api/chat", {"text": "another"})[0] == 409
        assert call(server, "GET", "/api/poll?after=0&rev=-1")[1]["state"] == "thinking"
        release.set()
        wait_for_query(server, first[1]["query_id"])

    def test_stop_drops_the_reply_and_says_so(self, server, booted):
        _, release = booted
        release.clear()
        _, body = call(server, "POST", "/api/chat", {"text": "never mind"})
        deadline = time.time() + 2
        while not daemon.is_query_running() and time.time() < deadline:
            time.sleep(0.01)
        assert call(server, "POST", "/api/stop")[0] == 200
        release.set()
        snap = wait_for_query(server, body["query_id"])
        assert snap["queries"][str(body["query_id"])]["status"] == "stopped"
        assert "Stopped." in [e["text"] for e in snap["entries"] if e["role"] == "notice"]

    def test_requests_are_redacted_before_the_phone_sees_them(self, server):
        _, body = call(server, "POST", "/api/chat", {"text": "email me at owner@example.com"})
        snap = wait_for_query(server, body["query_id"])
        user_turns = [e["text"] for e in snap["entries"] if e["role"] == "user"]
        assert user_turns and "owner@example.com" not in user_turns[0]
