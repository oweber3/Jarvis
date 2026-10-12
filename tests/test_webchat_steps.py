"""Tool steps in the web chat: stored with the reply they belong to, reloaded with the chat, served by the API.

The daemon is a fake with a real dialogue memory; the step log is a fresh one the test feeds the way
``run_tool_with_retries`` does. See ``webchat/webchat.spec.md``, Tool steps.
"""
import http.client
import json
import sqlite3
import time

import pytest

from fake_webchat_backend import FakeWebChatBackend
from jarvis.memory.chat_store import ChatStore
from jarvis.tools.step_log import StepLog
from jarvis.webchat.hub import ChatHub
from jarvis.webchat.server import ServerSettings, WebChatServer


@pytest.fixture
def log():
    return StepLog()


@pytest.fixture
def backend():
    return FakeWebChatBackend()


@pytest.fixture
def store(tmp_path):
    s = ChatStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


@pytest.fixture
def hub(backend, store, log):
    h = ChatHub(backend, store, sync_interval_sec=0.05, steps=log)
    h.sync()
    yield h
    h.stop()


def replies(store):
    chat_id = store.get_active_chat_id()
    return [m for m in store.messages(chat_id) if m.role == "assistant"]


def labels(message):
    return [(s["label"], s["status"], s.get("summary")) for s in message.steps]


class TestStorage:
    def test_a_message_keeps_its_steps(self, store):
        chat = store.create_chat()
        steps = [{"tool": "systemVolume", "status": "done", "label": "Set volume to 30%", "summary": None}]
        store.append_message(chat.id, "assistant", "Done", ts=1.0, source="typed", steps=steps)
        (message,) = store.messages(chat.id)
        assert message.steps == tuple(steps)
        assert store.last_messages(chat.id, 1)[0].steps == tuple(steps)

    def test_a_message_without_steps_has_none(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "user", "hi", ts=1.0)
        assert store.messages(chat.id)[0].steps == ()

    def test_a_database_from_before_steps_gains_the_column_and_keeps_its_messages(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE chats (id TEXT PRIMARY KEY, project_id TEXT, title TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL, updated_at REAL NOT NULL, last_mode TEXT NOT NULL DEFAULT '',
              last_model TEXT NOT NULL DEFAULT '');
            CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL, source TEXT NOT NULL DEFAULT 'typed',
              private INTEGER NOT NULL DEFAULT 0);
            INSERT INTO chats(id, created_at, updated_at) VALUES ('c1', 1, 1);
            INSERT INTO chat_messages(chat_id, role, content, ts) VALUES ('c1', 'assistant', 'old reply', 1);
        """)
        conn.commit()
        conn.close()
        migrated = ChatStore(path)
        try:
            (message,) = migrated.messages("c1")
            assert (message.content, message.steps) == ("old reply", ())
            migrated.append_message("c1", "assistant", "new", ts=2.0, steps=[
                {"tool": "a", "status": "done", "label": "A", "summary": None}])
            assert len(migrated.messages("c1")[1].steps) == 1
        finally:
            migrated.close()

    def test_opening_the_store_twice_is_harmless(self, tmp_path):
        ChatStore(str(tmp_path / "x.db")).close()
        ChatStore(str(tmp_path / "x.db")).close()

    def test_deleting_everything_removes_the_steps_too(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "assistant", "ok", ts=1.0, steps=[
            {"tool": "a", "status": "done", "label": "A", "summary": None}])
        store.delete_all()
        assert store.conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0


class TestTheReplyOwnsItsSteps:
    def test_a_typed_request_shows_the_calls_made_for_its_reply(self, hub, backend, store, log):
        backend.while_answering = lambda: (
            log.record("appControl", "done", "Opened Word"),
            log.record("systemVolume", "done", "Set volume to 30%", "Now 30%"))
        hub.submit("open word and turn it down")
        (reply,) = replies(store)
        assert labels(reply) == [("Opened Word", "done", None), ("Set volume to 30%", "done", "Now 30%")]

    def test_steps_carry_the_tool_name_and_status_only_besides_the_words(self, hub, backend, store, log):
        backend.while_answering = lambda: log.record("appControl", "failed", "App control")
        hub.submit("open word")
        (step,) = replies(store)[0].steps
        assert set(step) == {"tool", "status", "label", "summary"}
        assert (step["tool"], step["status"]) == ("appControl", "failed")

    def test_a_reply_with_no_calls_has_no_steps(self, hub, backend, store):
        hub.submit("hello")
        assert replies(store)[0].steps == ()

    def test_the_next_reply_does_not_repeat_the_last_ones_steps(self, hub, backend, store, log):
        backend.while_answering = lambda: log.record("appControl", "done", "Opened Word")
        hub.submit("open word")
        backend.while_answering = None
        hub.submit("thanks")
        first, second = replies(store)
        assert len(first.steps) == 1 and second.steps == ()

    def test_a_spoken_request_shows_its_steps_as_well(self, hub, backend, store, log):
        log.record("mediaControl", "done", "Paused the music")
        backend.speak("pause the music", "Paused.")
        hub.sync()
        assert labels(replies(store)[0]) == [("Paused the music", "done", None)]

    def test_the_outcome_of_a_confirmed_action_carries_the_step_that_ran(self, hub, backend, store, log):
        log.record("localFiles", "waiting", "Local files")
        backend.speak("delete the report", "Say yes to confirm.")
        hub.sync()
        log.record("localFiles", "done", "Local files")
        backend.memory.add_message("assistant", "Deleted it.")
        hub.sync()
        asked, outcome = replies(store)
        assert [s["status"] for s in asked.steps] == ["waiting"]
        assert [s["status"] for s in outcome.steps] == ["done"]

    def test_calls_from_before_the_chat_was_opened_are_not_shown(self, hub, backend, store, log):
        log.record("appControl", "done", "Opened Word")
        hub.new_chat()
        hub.submit("hello")
        assert replies(store)[0].steps == ()

    def test_calls_of_a_request_that_gave_no_reply_do_not_leak_into_the_next_one(self, hub, backend, store, log):
        backend.mode = "fail"
        backend.while_answering = None
        log.record("appControl", "done", "Opened Word")
        hub.submit("open word")  # fails before answering
        backend.mode = "reply"
        hub.submit("hello")
        assert replies(store)[0].steps == ()

    def test_a_private_reply_keeps_the_tool_but_not_what_it_did(self, hub, backend, store, log):
        def private_answer(text, on_complete):
            log.record("activityLog", "done", "Read 3 entries from 9 am", "3 entries")
            backend.memory.add_message("user", text, diary=False)
            backend.memory.add_message("assistant", backend.reply_text, diary=False)
            on_complete(backend.reply_text)

        backend._answer = private_answer
        hub.submit("what was I doing")
        (step,) = replies(store)[0].steps
        assert (step["label"], step["summary"]) == ("Activity log", None)

    def test_a_step_taken_after_a_reply_waits_for_the_reply_it_belongs_to(self, backend, store):
        later = StepLog(clock=lambda: time.time() + 60)  # clearly after the turns below
        hub = ChatHub(backend, store, steps=later)
        hub.sync()
        backend.speak("hi", "hello")
        later.record("appControl", "done", "Opened Word")
        hub.sync()
        assert replies(store)[0].steps == ()


class TestSnapshotAndApi:
    def test_the_poll_snapshot_includes_each_messages_steps(self, hub, backend, store, log):
        backend.while_answering = lambda: log.record("appControl", "done", "Opened Word")
        hub.submit("open word")
        messages = hub.snapshot(0)["messages"]
        assert [m["steps"] for m in messages if m["role"] == "user"] == [[]]
        assert [s["label"] for m in messages if m["role"] == "assistant" for s in m["steps"]] == ["Opened Word"]

    def test_the_private_flag_still_never_reaches_the_page(self, hub, backend):
        backend.speak("hi", "hello")
        hub.sync()
        for message in hub.snapshot(0)["messages"]:
            assert "private" not in message


@pytest.fixture
def served(tmp_path, backend, log):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>x</title>")
    store = ChatStore(str(tmp_path / "api.db"))
    hub = ChatHub(backend, store, sync_interval_sec=0.05, steps=log)
    server = WebChatServer(hub, ServerSettings(host="127.0.0.1", port=0, static_dir=static, poll_timeout_sec=0.3))
    server.start()
    hub.sync()
    yield type("Served", (), {"server": server, "store": store, "hub": hub})()
    server.stop()
    store.close()


def get(served, path):
    conn = http.client.HTTPConnection("127.0.0.1", served.server.port, timeout=5)
    conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
    conn.putheader("Host", f"127.0.0.1:{served.server.port}")
    conn.endheaders()
    response = conn.getresponse()
    body = json.loads(response.read())
    conn.close()
    return response.status, body


def test_opening_a_chat_returns_the_steps_each_reply_stored(served, backend, log):
    backend.while_answering = lambda: log.record("systemVolume", "done", "Set volume to 30%", "Now 30%")
    served.hub.submit("volume 30")
    chat_id = served.store.get_active_chat_id()
    status, body = get(served, f"/api/chats/{chat_id}")
    assert status == 200
    reply = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert reply["steps"] == [{"tool": "systemVolume", "status": "done", "label": "Set volume to 30%",
                               "summary": "Now 30%"}]
    user = [m for m in body["messages"] if m["role"] == "user"][0]
    assert user["steps"] == []


def test_the_long_poll_delivers_steps_with_the_reply(served, backend, log):
    backend.while_answering = lambda: log.record("appControl", "done", "Opened Word")
    served.hub.submit("open word")
    deadline = time.time() + 3
    body = {}
    while time.time() < deadline:
        _status, body = get(served, "/api/poll?rev=-1&after=0")
        if any(m["role"] == "assistant" for m in body["messages"]):
            break
    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    assert [s["label"] for s in assistant["steps"]] == ["Opened Word"]
