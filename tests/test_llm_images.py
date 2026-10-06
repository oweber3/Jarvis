"""Images on chat messages reach only backends and models that can see them."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jarvis.llm import OllamaBackend
from jarvis.llm.backend import strip_nonstandard_message_fields
from jarvis.llm.openai_compatible import OpenAICompatibleBackend

pytestmark = pytest.mark.unit

CAPABILITIES = {"seeing": ["completion", "vision", "tools"], "blind": ["completion", "tools"]}


@pytest.fixture
def ollama_server():
    recorded = {"chat": [], "show": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                recorded["show"].append(body)
                name = body.get("model") or body.get("name")
                if name not in CAPABILITIES:
                    self.reply({"error": "model not found"}, status=404)
                    return
                self.reply({"capabilities": CAPABILITIES[name]})
                return
            recorded["chat"].append(body)
            self.reply({"message": {"role": "assistant", "content": "ok"}})

        def reply(self, data, status=200):
            content = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", recorded
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_ollama_reports_vision_from_the_model_capabilities_and_asks_once(ollama_server):
    url, recorded = ollama_server
    backend = OllamaBackend(url)
    assert backend.supports_images("seeing") is True
    assert backend.supports_images("seeing") is True
    assert backend.supports_images("blind") is False
    assert len(recorded["show"]) == 2


def test_an_unknown_or_unreachable_model_is_treated_as_unable_to_see(ollama_server):
    url, _ = ollama_server
    assert OllamaBackend(url).supports_images("missing") is False
    assert OllamaBackend("http://127.0.0.1:9").supports_images("seeing") is False


def test_ollama_sends_message_images(ollama_server):
    url, recorded = ollama_server
    messages = [{"role": "user", "content": "look"},
                {"role": "tool", "tool_call_id": "1", "tool_name": "screenshot", "content": "text",
                 "images": ["QUJD"]}]
    OllamaBackend(url).chat("seeing", messages)
    sent = recorded["chat"][0]["messages"]
    assert sent[1]["images"] == ["QUJD"] and "tool_name" not in sent[1]


def test_the_standard_payload_never_carries_images():
    cleaned = strip_nonstandard_message_fields([{"role": "user", "content": "x", "images": ["QUJD"]}])
    assert cleaned == [{"role": "user", "content": "x"}]


def test_an_openai_compatible_server_is_never_assumed_to_see():
    assert OpenAICompatibleBackend("http://127.0.0.1:9").supports_images("anything") is False
