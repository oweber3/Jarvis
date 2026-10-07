"""The web chat over the real daemon wiring: the actual text path, query lock and dialogue memory.

Only the reply engine (the model) is replaced. Everything between the HTTP request and the stored chat is
the production code. See ``webchat/webchat.spec.md``.
"""
import dataclasses
import http.client
import json
import threading
import time

import pytest

from jarvis import daemon
from jarvis.memory.chat_store import ChatStore
from jarvis.memory.conversation import DialogueMemory
from jarvis.webchat.backend import DaemonBackend
from jarvis.webchat.hub import ChatHub
from jarvis.webchat.server import ServerSettings, WebChatServer


@dataclasses.dataclass(frozen=True)
class Cfg:
    llm_provider: str = "ollama"
    llm_chat_model: str = "gemma4:12b"
    ollama_chat_model: str = "gemma4:12b"
    fast_model: str = "qwen3.5:0.8b"
    embedding_model: str = "nomic-embed-text"
    tool_model: str = ""
    use_stdin: bool = False
    llm_chat_timeout_sec: float = 5.0


@pytest.fixture
def world(tmp_path, monkeypatch):
    memory = DialogueMemory()
    monkeypatch.setattr(daemon, "_global_dialogue_memory", memory)
    monkeypatch.setattr(daemon, "_global_cfg", Cfg())
    monkeypatch.setattr(daemon, "_global_db", object())
    monkeypatch.setattr(daemon, "_global_stop_requested", False)
    monkeypatch.setattr(daemon, "_chat_query_lock", threading.Lock())
    monkeypatch.setattr(daemon, "update_diary_from_dialogue_memory", lambda **kw: 1)
    gate = threading.Event()
    gate.set()
    seen = []

    def engine(db, cfg, tts, text, dialogue_memory, language, quiet, origin):
        gate.wait(5)
        seen.append((text, origin, tts))
        dialogue_memory.add_message("user", text)
        dialogue_memory.add_message("assistant", f"Reply to: {text}")
        return f"Reply to: {text}"

    monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", engine)

    store = ChatStore(str(tmp_path / "jarvis.db"))
    hub = ChatHub(DaemonBackend(), store, sync_interval_sec=0.05)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<title>x</title>")
    server = WebChatServer(hub, ServerSettings(host="127.0.0.1", port=0, static_dir=static, poll_timeout_sec=0.3))
    server.start()
    yield type("World", (), {"server": server, "store": store, "memory": memory, "gate": gate, "seen": seen})()
    gate.set()
    server.stop()
    store.close()


def call(world, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", world.server.port, timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None or method != "GET" else {}
    conn.request(method, path, json.dumps(body if body is not None else {}) if headers else None, headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, (json.loads(data) if data else {})


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def open_chat_texts(world):
    return [(m.role, m.content, m.source) for m in world.store.messages(world.store.get_active_chat_id())]


def test_a_typed_message_runs_the_real_text_path_and_is_stored(world):
    status, body = call(world, "POST", "/api/chat", {"text": "what is a nebula"})
    assert status == 202
    assert wait_for(lambda: len(open_chat_texts(world)) == 2)
    text, origin, tts = world.seen[0]
    assert (text, origin, tts) == ("what is a nebula", "chat", None)  # text chat never speaks
    assert open_chat_texts(world)[0] == ("user", "what is a nebula", "typed")


def test_voice_and_typed_turns_land_in_the_same_open_chat(world):
    call(world, "POST", "/api/chat", {"text": "typed one"})
    assert wait_for(lambda: len(open_chat_texts(world)) == 2)
    assert wait_for(lambda: not call(world, "GET", "/api/poll?rev=-1&after=0")[1]["busy_query"])
    world.memory.add_message("user", "a spoken request")
    world.memory.add_message("assistant", "a spoken answer")
    assert wait_for(lambda: len(open_chat_texts(world)) == 4)
    sources = [source for _r, _c, source in open_chat_texts(world)]
    assert sources == ["typed", "typed", "voice", "voice"]


def test_a_request_while_another_runs_is_refused_as_busy(world):
    world.gate.clear()
    assert call(world, "POST", "/api/chat", {"text": "slow"})[0] == 202
    status, body = call(world, "POST", "/api/chat", {"text": "second"})
    assert status == 409
    world.gate.set()
    assert wait_for(lambda: len(open_chat_texts(world)) == 2)


def test_opening_another_chat_swaps_the_real_conversation(world):
    call(world, "POST", "/api/chat", {"text": "about trains"})
    assert wait_for(lambda: len(open_chat_texts(world)) == 2)
    trains = world.store.get_active_chat_id()
    status, body = call(world, "POST", "/api/chats", {})
    assert status == 201 and world.memory.all_messages() == []
    call(world, "POST", "/api/chat", {"text": "about boats"})
    assert wait_for(lambda: len(open_chat_texts(world)) == 2)
    assert call(world, "POST", f"/api/chats/{trains}/open")[0] == 200
    assert [m["content"] for m in world.memory.all_messages()] == ["about trains", "Reply to: about trains"]
    # the restored turns are not summarised into the diary a second time
    assert world.memory.get_pending_chunks() == []


def test_switching_is_refused_while_a_request_runs(world):
    call(world, "POST", "/api/chat", {"text": "about trains"})
    assert wait_for(lambda: len(open_chat_texts(world)) == 2)
    trains = world.store.get_active_chat_id()
    call(world, "POST", "/api/chats", {})
    world.gate.clear()
    call(world, "POST", "/api/chat", {"text": "slow"})
    assert call(world, "POST", f"/api/chats/{trains}/open")[0] == 409
    world.gate.set()


def test_the_outcome_of_a_confirmed_action_arrives_in_the_chat(world):
    call(world, "POST", "/api/chat", {"text": "uninstall the app"})
    assert wait_for(lambda: len(open_chat_texts(world)) == 2)
    daemon.deliver_chat_confirmed_result("Uninstalled the app.", True)
    assert wait_for(lambda: len(open_chat_texts(world)) == 3)
    assert open_chat_texts(world)[-1] == ("assistant", "Uninstalled the app.", "confirmed")


def test_stop_cancels_the_real_query_and_drops_its_reply(world):
    world.gate.clear()
    call(world, "POST", "/api/chat", {"text": "slow"})
    assert call(world, "POST", "/api/stop", {})[0] == 200
    world.gate.set()
    assert wait_for(lambda: any(n["kind"] == "stopped" for n in call(world, "GET", "/api/poll?rev=-1&after=0")[1]["notices"]))


def test_the_local_model_switch_reaches_the_daemons_settings(world, monkeypatch):
    class Runtime:
        def list_models(self, **_kw):
            return ["gemma4:12b", "qwen3.5:9b"]

        def warm_up(self, *a, **k):
            return True

        def release(self, *a, **k):
            return True

    monkeypatch.setattr("jarvis.llm.get_llm_backend", lambda cfg: Runtime())
    monkeypatch.setattr("jarvis.config.update_config_values", lambda values: True)
    status, _ = call(world, "POST", "/api/model", {"kind": "local", "value": "qwen3.5:9b"})
    assert status == 200
    assert daemon._global_cfg.llm_chat_model == "qwen3.5:9b"
    listed = call(world, "GET", "/api/models")[1]
    assert listed["current"] == "qwen3.5:9b"
