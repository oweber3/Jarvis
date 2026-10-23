"""Phone access against the real daemon text path: submit_text_query, the shared lock and dialogue memory.

The reply engine is replaced by a stand-in that records turns like the real one, so nothing reaches a model.
The speech model and the PC's voice are stand-ins too, so a spoken phone request can be followed all the way
to its audio without touching a microphone or a speaker.
"""
import http.client
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from fake_remote_backend import FakeRemoteBackend
from jarvis import daemon
from jarvis.memory.chat_store import ChatStore
from jarvis.memory.conversation import DialogueMemory
from jarvis.remote.backend import DaemonBackend as RemoteBackend
from jarvis.remote.pairing import DeviceStore
from jarvis.remote.server import RemoteServer, ServerSettings
from jarvis.remote.tls import TLSConfig
from jarvis.webchat.backend import DaemonBackend as ChatBackend
from jarvis.webchat.hub import ChatHub
from tls_fixtures import client_context, make_certificate

SECOND = b"\x10\x00" * 16000


class PcVoice:
    """The PC's voice engine: renders text to samples, and records anything played aloud."""

    def __init__(self):
        self.spoken, self.rendered = [], []

    def render(self, text):
        self.rendered.append(text)
        return np.ones(2205, dtype=np.int16), 22050

    def speak(self, text, *args, **kwargs):
        self.spoken.append(text)


@pytest.fixture
def booted(monkeypatch):
    memory = DialogueMemory()
    monkeypatch.setattr(daemon, "_global_dialogue_memory", memory)
    monkeypatch.setattr(daemon, "_global_cfg", SimpleNamespace(dictation_enabled=True, llm_chat_model="m", llm_provider="ollama"))
    monkeypatch.setattr(daemon, "_global_db", object())
    monkeypatch.setattr(daemon, "_global_stop_requested", False)
    voice = PcVoice()
    monkeypatch.setattr(daemon, "_global_tts_engine", voice)
    release = threading.Event()
    release.set()
    tts_seen = []

    def fake_engine(db, cfg, tts, text, dialogue_memory, language=None, quiet=False, **_):
        tts_seen.append(tts)
        assert quiet is True
        release.wait(5)
        from jarvis.utils.redact import redact
        dialogue_memory.add_message("user", redact(text))
        dialogue_memory.add_message("assistant", f"Echo: {text}")
        return f"Echo: {text}"

    monkeypatch.setattr("jarvis.reply.engine.run_reply_engine", fake_engine)
    return SimpleNamespace(memory=memory, release=release, voice=voice, tts_seen=tts_seen)


def make(booted, tmp_path, tls=None):
    chat_store = ChatStore(str(tmp_path / "jarvis.db"))
    hub = ChatHub(ChatBackend(), chat_store, sync_interval_sec=0.05)
    store = DeviceStore(tmp_path)
    # Speech in is faked (no microphone model here); everything else about the remote backend is real.
    backend = RemoteBackend()
    backend.voice_reason = lambda: None
    backend.transcribe = lambda samples: "turn the volume down"
    backend.is_busy = lambda: daemon.is_query_running()
    server = RemoteServer(hub, store, backend, ServerSettings(host="127.0.0.1", port=0, poll_timeout_sec=2.0,
                                                              tls=tls))
    server.start()
    code = store.start_pairing()
    server.chat_store = chat_store
    return server, store, code


@pytest.fixture
def server(booted, tmp_path):
    server, store, code = make(booted, tmp_path)
    server.context = None
    server.token = call(server, "POST", "/api/pair", {"code": code, "name": "phone"})[1]["token"]
    yield server
    server.stop()
    server.chat_store.close()


@pytest.fixture
def secure(booted, tmp_path):
    cert, key = make_certificate(tmp_path)
    server, store, code = make(booted, tmp_path, TLSConfig(str(cert), str(key)))
    server.context = client_context(cert)
    server.token = call(server, "POST", "/api/pair", {"code": code, "name": "phone"})[1]["token"]
    yield server
    server.stop()
    server.chat_store.close()


def call(server, method, path, body=None, raw=None, binary=False):
    context = getattr(server, "context", None)
    conn = (http.client.HTTPSConnection("127.0.0.1", server.port, timeout=10, context=context) if context
            else http.client.HTTPConnection("127.0.0.1", server.port, timeout=10))
    headers = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
    if getattr(server, "token", None):
        headers["Authorization"] = f"Bearer {server.token}"
    payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data if binary else (json.loads(data) if data else None)


def wait_until(check, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("did not happen in time")


def chat_messages(server):
    return [(m.role, m.content, m.source) for m in server.chat_store.messages(server.chat_store.get_active_chat_id())]


@pytest.mark.integration
class TestPhoneThroughTheDaemon:
    def test_a_phone_request_is_answered_and_joins_the_conversation(self, server, booted):
        status, body = call(server, "POST", "/api/chat", {"text": "volume up"})
        assert status == 202
        wait_until(lambda: len(chat_messages(server)) == 2)
        assert chat_messages(server) == [("user", "volume up", "typed"), ("assistant", "Echo: volume up", "typed")]
        assert [m["content"] for m in booted.memory.all_messages()] == ["volume up", "Echo: volume up"]

    def test_a_second_request_while_one_runs_is_busy(self, server, booted):
        booted.release.clear()
        assert call(server, "POST", "/api/chat", {"text": "slow one"})[0] == 202
        wait_until(daemon.is_query_running)
        assert call(server, "POST", "/api/chat", {"text": "another"})[0] == 409
        assert call(server, "GET", "/api/poll?after=0&rev=-1")[1]["state"] == "thinking"
        booted.release.set()
        wait_until(lambda: len(chat_messages(server)) == 2)

    def test_stop_drops_the_reply_and_says_so(self, server, booted):
        booted.release.clear()
        call(server, "POST", "/api/chat", {"text": "never mind"})
        wait_until(daemon.is_query_running)
        assert call(server, "POST", "/api/stop")[0] == 200
        booted.release.set()
        snap = wait_until(lambda: [n for n in call(server, "GET", "/api/poll?after=0&rev=-1")[1]["notices"]
                                   if n["kind"] == "stopped"] or None)
        assert snap

    def test_requests_are_redacted_before_the_phone_sees_them(self, server):
        call(server, "POST", "/api/chat", {"text": "email me at owner@example.com"})
        wait_until(lambda: chat_messages(server))
        assert "owner@example.com" not in chat_messages(server)[0][1]

    def test_a_phone_request_runs_without_the_pcs_voice(self, server, booted):
        call(server, "POST", "/api/chat", {"text": "hello", "speak": True})
        wait_until(lambda: len(chat_messages(server)) == 2)
        assert booted.tts_seen == [None]


@pytest.mark.integration
class TestSpokenRequestThroughTheDaemon:
    def test_it_is_heard_answered_and_spoken_on_the_phone_only(self, secure, booted):
        status, body = call(secure, "POST", "/api/voice", raw=SECOND)
        assert status == 202 and body["text"] == "turn the volume down"
        wait_until(lambda: len(chat_messages(secure)) == 2)
        assert chat_messages(secure) == [("user", "turn the volume down", "voice"),
                                         ("assistant", "Echo: turn the volume down", "voice")]
        ready = wait_until(lambda: [e for e in call(secure, "GET", "/api/poll?after=0&rev=-1")[1]["phone"]["voice"][
            "audio"].values() if e["status"] == "ready"])
        status, clip = call(secure, "GET", ready[0]["url"], binary=True)
        assert status == 200 and clip[:4] == b"RIFF"
        # Rendered into memory with the PC's voice engine, never played at the PC.
        assert booted.voice.rendered == ["Echo: turn the volume down"]
        assert booted.voice.spoken == [] and booted.tts_seen == [None]

    def test_the_pc_stays_silent_for_a_typed_request_that_asks_for_audio_too(self, secure, booted):
        call(secure, "POST", "/api/chat", {"text": "hello", "speak": True})
        wait_until(lambda: booted.voice.rendered)
        assert booted.voice.spoken == []

    def test_plain_http_cannot_carry_a_spoken_request(self, server, booted):
        status, body = call(server, "POST", "/api/voice", raw=SECOND)
        assert (status, body["error"]) == (503, "no_https")
        assert booted.memory.all_messages() == []
