"""A chat call that the Stop button can end: it returns the same answer as a plain call when nothing
stops it, and ends promptly, with the model's work dropped, when the cancel event is set.

Ollama is imitated by a real local HTTP server so the abort is a real closed connection. See
``llm/llm.spec.md``, Cancelling a chat call.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jarvis.llm import ToolsNotSupportedError
from jarvis.llm.errors import RequestCancelled
from jarvis.llm.ollama import OllamaBackend
from jarvis.llm.openai_compatible import OpenAICompatibleBackend


class FakeOllama:
    """``/api/chat`` that waits ``first_delay`` before answering and then sends ``chunks`` ``gap`` apart."""

    def __init__(self, chunks=(), *, first_delay=0.0, gap=0.0, status=200):
        self.chunks = list(chunks)
        self.first_delay = first_delay
        self.gap = gap
        self.status = status
        self.requests = []
        self.disconnected = threading.Event()
        self.finished = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                time.sleep(owner.first_delay)
                try:
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    for chunk in owner.chunks:
                        self.wfile.write((json.dumps(chunk) + "\n").encode())
                        self.wfile.flush()
                        time.sleep(owner.gap)
                    owner.finished.set()
                except (BrokenPipeError, ConnectionError, OSError):
                    owner.disconnected.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def served():
    made = []

    def make(*args, **kwargs):
        fake = FakeOllama(*args, **kwargs)
        made.append(fake)
        return fake

    yield make
    for fake in made:
        fake.close()


def words(*parts):
    chunks = [{"message": {"role": "assistant", "content": part}, "done": False} for part in parts]
    chunks.append({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                   "eval_count": len(parts)})
    return chunks


MESSAGES = [{"role": "user", "content": "hi"}]


def ask(fake, cancel, **kwargs):
    kwargs.setdefault("timeout_sec", 10.0)
    return OllamaBackend(fake.url).chat_cancellable("m", MESSAGES, cancel, **kwargs)


class TestAnAnswerNothingStops:
    def test_the_streamed_pieces_come_back_as_one_answer(self, served):
        fake = served(words("Hel", "lo"))
        reply = ask(fake, threading.Event())
        assert reply["message"]["content"] == "Hello"
        assert reply["message"]["role"] == "assistant"
        assert reply["done"] is True and reply["done_reason"] == "stop"

    def test_thinking_and_tool_calls_are_kept(self, served):
        call = {"function": {"name": "getWeather", "arguments": {"place": "Rome"}}}
        fake = served([
            {"message": {"role": "assistant", "content": "", "thinking": "Let me "}, "done": False},
            {"message": {"role": "assistant", "content": "", "thinking": "check."}, "done": False},
            {"message": {"role": "assistant", "content": "", "tool_calls": [call]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        ])
        message = ask(fake, threading.Event())["message"]
        assert message["thinking"] == "Let me check."
        assert message["tool_calls"] == [call]

    def test_it_sends_the_same_request_a_plain_call_does_apart_from_streaming(self, served):
        fake = served(words("ok"))
        tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
        ask(fake, threading.Event(), tools=tools, thinking=True, extra_options={"temperature": 0.2})
        sent = fake.requests[0]
        assert sent["stream"] is True and sent["model"] == "m" and sent["tools"] == tools
        assert sent["think"] is True and sent["options"]["temperature"] == 0.2
        assert sent["messages"] == MESSAGES

    def test_a_model_that_rejects_tools_says_so(self, served):
        fake = served(status=400)
        with pytest.raises(ToolsNotSupportedError):
            ask(fake, threading.Event(), tools=[{"type": "function", "function": {"name": "t"}}])

    def test_a_server_error_gives_no_answer(self, served):
        assert ask(served(status=500), threading.Event()) is None

    def test_a_server_that_stays_silent_past_the_timeout_gives_no_answer(self, served):
        fake = served(words("late"), first_delay=3.0)
        started = time.monotonic()
        assert ask(fake, threading.Event(), timeout_sec=0.5) is None
        assert time.monotonic() - started < 2.5

    def test_an_answer_that_keeps_coming_past_the_timeout_gives_no_answer(self, served):
        fake = served(words(*["w"] * 40), gap=0.05)
        assert ask(fake, threading.Event(), timeout_sec=0.5) is None


class TestStopping:
    def stop_after(self, seconds):
        cancel = threading.Event()
        threading.Timer(seconds, cancel.set).start()
        return cancel

    def test_stopping_while_the_answer_streams_ends_the_call_and_the_connection(self, served):
        fake = served(words(*["w"] * 100), gap=0.1)
        started = time.monotonic()
        with pytest.raises(RequestCancelled):
            ask(fake, self.stop_after(0.35))
        assert time.monotonic() - started < 2.0
        assert fake.disconnected.wait(3.0), "the server was left generating for nobody"
        assert not fake.finished.is_set()

    def test_stopping_while_the_model_has_not_started_to_answer_ends_the_call(self, served):
        fake = served(words("never"), first_delay=8.0)
        started = time.monotonic()
        with pytest.raises(RequestCancelled):
            ask(fake, self.stop_after(0.3), timeout_sec=30.0)
        assert time.monotonic() - started < 2.0

    def test_a_call_made_after_the_stop_never_reaches_the_server(self, served):
        fake = served(words("never"))
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(RequestCancelled):
            ask(fake, cancel)
        assert fake.requests == []

    def test_the_stop_is_not_an_ordinary_error(self):
        # Code that guards a model call with ``except Exception`` must not swallow a Stop.
        assert not issubclass(RequestCancelled, Exception)


class TestBackendsWithoutAnAbort:
    """A backend that cannot drop its request still frees the caller at once."""

    def backend(self, chat):
        backend = OpenAICompatibleBackend("http://127.0.0.1:9/v1")
        backend.chat = chat
        return backend

    def test_an_answer_nothing_stops_comes_back(self):
        backend = self.backend(lambda *a, **k: {"message": {"content": "hi"}})
        assert backend.chat_cancellable("m", MESSAGES, threading.Event()) == {"message": {"content": "hi"}}

    def test_a_failure_of_the_call_is_raised(self):
        def boom(*a, **k):
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            self.backend(boom).chat_cancellable("m", MESSAGES, threading.Event())

    def test_stopping_frees_the_caller_while_the_call_is_still_running(self):
        release = threading.Event()
        backend = self.backend(lambda *a, **k: release.wait(10) and {"message": {"content": "late"}})
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        started = time.monotonic()
        try:
            with pytest.raises(RequestCancelled):
                backend.chat_cancellable("m", MESSAGES, cancel)
            assert time.monotonic() - started < 2.0
        finally:
            release.set()
