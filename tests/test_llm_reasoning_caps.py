"""A length cap never leaves a model that reasons regardless of ``think: false`` without an answer.

A fake Ollama ``/api/chat`` plays three kinds of model, as measured against a real server:
gpt-oss reasons even when asked not to, and its reasoning counts against ``num_predict``;
qwen3 obeys ``think: false`` and starts reasoning when given a level; a third reasons
regardless and refuses reasoning levels.
"""

import pytest
import requests

import jarvis.llm.ollama as ollama
from jarvis.llm.ollama import OllamaBackend

pytestmark = pytest.mark.unit

ANSWER = "weather"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# Reasoning tokens each kind of model spends for a given ``think`` value; None refuses the value.
MODELS = {
    "always-reasons": {False: 400, True: 400, "low": 20},
    "obeys": {False: 0, True: 400, "low": 400},
    "refuses-levels": {False: 400, True: 400, "low": None},
}


class FakeOllama:
    def __init__(self):
        self.requests = []

    def __call__(self, url, json=None, timeout=None, **_):
        self.requests.append(json)
        model, think = json["model"], json.get("think")
        reasoning = MODELS[model].get(think)
        if reasoning is None:
            return FakeResponse(400, {"error": f"think value {think!r} is not supported for this model"})
        cap = json.get("options", {}).get("num_predict")
        needed = reasoning + 2
        if cap is not None and cap < needed:
            message = {"role": "assistant", "content": "", "thinking": "x" * (4 * min(cap, reasoning))}
            return FakeResponse(200, {"message": message, "done": True, "done_reason": "length"})
        message = {"role": "assistant", "content": ANSWER}
        if reasoning:
            message["thinking"] = "x" * (4 * reasoning)
        return FakeResponse(200, {"message": message, "done": True, "done_reason": "stop"})


@pytest.fixture
def server(monkeypatch):
    ollama._REASONS_REGARDLESS.clear()
    fake = FakeOllama()
    monkeypatch.setattr(ollama.requests, "post", fake)
    yield fake
    ollama._REASONS_REGARDLESS.clear()


def ask(model, *, cap=150, thinking=False, base_url="http://127.0.0.1:11434"):
    return OllamaBackend(base_url).direct(model, "Classify the request.", "will it rain tomorrow",
                                          thinking=thinking, max_tokens=cap)


def test_a_model_that_reasons_regardless_still_answers_a_capped_call(server):
    assert ask("always-reasons") == ANSWER


def test_once_learned_the_model_is_asked_for_low_reasoning_in_a_single_request(server):
    ask("always-reasons")
    server.requests.clear()

    assert ask("always-reasons") == ANSWER
    assert len(server.requests) == 1
    assert server.requests[0]["think"] == "low"


def test_a_model_that_obeys_think_false_is_left_exactly_as_asked(server):
    for _ in range(2):
        assert ask("obeys", cap=50) == ANSWER
    assert [(r["think"], r["options"]["num_predict"]) for r in server.requests] == [(False, 50), (False, 50)]


def test_a_model_that_refuses_reasoning_levels_still_answers_and_is_not_sent_levels_again(server):
    assert ask("refuses-levels") == ANSWER
    server.requests.clear()

    assert ask("refuses-levels") == ANSWER
    assert all(r["think"] is False for r in server.requests)
    assert len(server.requests) == 1


def test_an_uncapped_call_is_not_given_a_cap(server):
    ask("always-reasons")
    server.requests.clear()
    assert ask("always-reasons", cap=None) == ANSWER
    assert "num_predict" not in server.requests[0]["options"]


def test_a_call_that_asks_for_reasoning_keeps_it(server):
    ask("always-reasons")
    server.requests.clear()
    assert ask("always-reasons", cap=None, thinking=True) == ANSWER
    assert server.requests[0]["think"] is True


def test_what_is_learned_about_a_model_belongs_to_its_server(server):
    ask("always-reasons", base_url="http://127.0.0.1:11434")
    server.requests.clear()
    ask("always-reasons", base_url="http://192.168.1.20:11434")
    assert server.requests[0]["think"] is False
