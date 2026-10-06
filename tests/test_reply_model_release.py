"""Leaving local reply mode unloads the local models only local replies use.

Codex and Claude never page in the chat or tool model, so after a switch away from local those
models would otherwise hold memory (often most of the GPU) for Ollama's whole keep-alive. Models
the fast tier or embeddings still use stay loaded, and nothing that is not loaded gets touched.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from jarvis.bridge import modes


class FakeOllama:
    """Reports ``loaded`` models on /api/ps and records unload requests (``keep_alive`` 0)."""

    def __init__(self, loaded):
        self.loaded = list(loaded)
        self.unloaded = []
        self.loads = []
        self.changed = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _reply(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/ps"):
                    self._reply({"models": [{"name": m, "model": m} for m in fake.loaded]})
                else:
                    self._reply({"version": "0.12.0"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                model = payload.get("model")
                if payload.get("keep_alive") in (0, "0", "0s"):
                    fake.unloaded.append(model)
                    if model in fake.loaded:
                        fake.loaded.remove(model)
                else:
                    fake.loads.append(model)
                fake.changed.set()
                self._reply({"model": model, "done": True, "message": {"role": "assistant", "content": ""}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeService:
    def prepare(self):
        return None

    def is_busy(self):
        return False

    def cancel_active(self, reason):
        return False

    def close(self):
        pass


def _cfg(url, **kw):
    base = dict(reply_mode="local", codex_enabled=True, claude_enabled=True, llm_provider="ollama",
                ollama_base_url=url, llm_chat_model="big-chat", fast_model="small-fast", tool_model="",
                embedding_model="embed-text", llm_num_ctx=8192, llm_keep_alive="30m", low_power_mode=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def clean():
    modes.reset()
    yield
    modes.reset()


def _start(cfg):
    factories = {"codex": lambda c: FakeService(), "claude": lambda c: FakeService()}
    return modes.start(cfg, factories=factories, save=lambda values: True)


def _settle():
    for thread in threading.enumerate():
        if thread.name == "reply-model-release":
            thread.join(5)


@pytest.mark.unit
@pytest.mark.parametrize("cloud", [modes.CODEX, modes.CLAUDE])
def test_switching_to_a_cloud_mode_unloads_the_local_chat_and_tool_models(cloud):
    ollama = FakeOllama(["big-chat", "tool-caller", "small-fast", "embed-text"])
    try:
        _start(_cfg(ollama.url, tool_model="tool-caller"))
        assert modes.switch(cloud).ok
        _settle()
        assert sorted(ollama.unloaded) == ["big-chat", "tool-caller"]
        assert sorted(ollama.loaded) == ["embed-text", "small-fast"]
    finally:
        ollama.close()


@pytest.mark.unit
def test_a_chat_model_that_is_also_the_fast_model_stays_loaded():
    ollama = FakeOllama(["shared-model", "embed-text"])
    try:
        _start(_cfg(ollama.url, llm_chat_model="shared-model", fast_model="shared-model"))
        assert modes.switch(modes.CODEX).ok
        _settle()
        assert ollama.unloaded == []
        assert sorted(ollama.loaded) == ["embed-text", "shared-model"]
    finally:
        ollama.close()


@pytest.mark.unit
def test_a_model_that_is_not_loaded_is_left_alone():
    ollama = FakeOllama(["small-fast"])
    try:
        _start(_cfg(ollama.url))
        assert modes.switch(modes.CODEX).ok
        _settle()
        assert ollama.unloaded == []
        assert ollama.loads == []
    finally:
        ollama.close()


@pytest.mark.unit
def test_returning_to_local_or_moving_between_cloud_modes_unloads_nothing():
    ollama = FakeOllama(["small-fast", "embed-text"])
    try:
        _start(_cfg(ollama.url, reply_mode="codex"))
        assert modes.switch(modes.CLAUDE).ok
        ollama.loaded.append("big-chat")
        assert modes.switch(modes.LOCAL).ok
        _settle()
        assert ollama.unloaded == []
        assert "big-chat" in ollama.loaded
    finally:
        ollama.close()


@pytest.mark.unit
def test_a_runtime_that_manages_its_own_residency_is_not_asked_to_unload():
    server = FakeOllama(["big-chat"])
    try:
        _start(_cfg(server.url, llm_provider="openai_compatible", llm_base_url=server.url))
        assert modes.switch(modes.CODEX).ok
        _settle()
        assert server.unloaded == []
        assert server.loaded == ["big-chat"]
    finally:
        server.close()


@pytest.mark.unit
def test_an_unreachable_runtime_does_not_disturb_the_switch():
    _start(_cfg("http://127.0.0.1:9"))
    result = modes.switch(modes.CODEX)
    _settle()
    assert result.ok
    assert modes.active_mode() == modes.CODEX
