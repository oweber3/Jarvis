"""Load-affecting Ollama options belong to the backend, across a whole turn."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.llm import OllamaBackend, get_llm_backend, resolve_model, Tier


@pytest.fixture
def ollama_server():
    recorded = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply({"version": "test"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            recorded.append(body)
            self.reply({"message": {"content": "ok"}}, stream=body.get("stream"))

        def reply(self, data, stream=False):
            content = (json.dumps(data) + ("\n" if stream else "")).encode()
            self.send_response(200)
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


def settings(tmp_path, monkeypatch, overrides):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"_config_version": 3, **overrides}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return load_settings(), path


@pytest.mark.parametrize("low_power, residency", [(False, "30m"), (True, "1m")])
def test_simulated_turn_has_one_load_shape(ollama_server, tmp_path, monkeypatch, low_power, residency):
    url, recorded = ollama_server
    cfg, _ = settings(tmp_path, monkeypatch, {
        "ollama_base_url": url, "ollama_chat_model": "shared", "fast_model": "shared",
        "llm_num_ctx": 12288, "low_power_mode": low_power,
    })
    backend = get_llm_backend(cfg)
    fast, chat = resolve_model(cfg, Tier.FAST), resolve_model(cfg, Tier.CHAT)
    assert backend.warm_up(fast)
    assert backend.chat(fast, [{"role": "user", "content": "judge"}])
    assert backend.direct(fast, "router", "query", max_tokens=50) == "ok"
    assert backend.direct(chat, "planner", "query", max_tokens=150) == "ok"
    assert backend.streaming(chat, "summary", "query") == "ok"
    assert backend.chat(chat, [{"role": "user", "content": "reply"}])
    assert {r["options"].get("num_ctx") for r in recorded} == {cfg.llm_num_ctx}
    assert {r.get("keep_alive") for r in recorded} == {residency}
    assert {r.get("think") for r in recorded} == {False}


@pytest.mark.parametrize("configured, low_power, residency", [
    ("2h", False, "2h"),
    (-1, False, -1),
    ("3600", False, 3600),
    ("2h", True, "1m"),
])
def test_configured_keep_alive_is_the_residency_of_every_request(
        ollama_server, tmp_path, monkeypatch, configured, low_power, residency):
    url, recorded = ollama_server
    cfg, _ = settings(tmp_path, monkeypatch, {
        "ollama_base_url": url, "llm_keep_alive": configured, "low_power_mode": low_power,
    })
    backend = get_llm_backend(cfg)
    assert backend.warm_up("shared")
    assert backend.direct("shared", "router", "query") == "ok"
    assert backend.chat("shared", [{"role": "user", "content": "reply"}])
    assert {r.get("keep_alive") for r in recorded} == {residency}


@pytest.mark.parametrize("invalid", ["forever", "", "5 minutes", True, {"m": 5},
                                     "30 m", "2 h", "1e3s", "infs", "nanm", "30_0m", "m"])
def test_invalid_keep_alive_uses_the_default(tmp_path, monkeypatch, invalid):
    cfg, _ = settings(tmp_path, monkeypatch, {"llm_keep_alive": invalid})
    assert cfg.llm_keep_alive == get_default_config()["llm_keep_alive"]


@pytest.mark.parametrize("duration", ["30m", "1h30m", "1.5h", "250ms", "-1m", ".5h", "10µs"])
def test_go_style_durations_are_kept(tmp_path, monkeypatch, duration):
    """Ollama parses keep_alive with Go's time.ParseDuration; what it accepts is kept as written."""
    cfg, _ = settings(tmp_path, monkeypatch, {"llm_keep_alive": duration})
    assert cfg.llm_keep_alive == duration


def test_generation_options_cannot_override_backend_shape(ollama_server):
    url, recorded = ollama_server
    backend = OllamaBackend(url)
    backend.chat("shared", [], extra_options={
        "num_ctx": 4096, "keep_alive": "0", "temperature": 0.2,
        "options": {"num_ctx": 16384, "keep_alive": "1m", "max_tokens": 20},
    })
    assert recorded[-1]["options"]["num_ctx"] == get_default_config()["llm_num_ctx"]
    assert recorded[-1]["keep_alive"] == "30m"
    assert "keep_alive" not in recorded[-1]["options"]
    assert recorded[-1]["options"]["temperature"] == 0.2
    assert recorded[-1]["options"]["num_predict"] == 20


def test_distinct_model_targets_can_own_distinct_contexts(ollama_server):
    url, recorded = ollama_server
    fast = OllamaBackend(url, num_ctx=8192)
    chat = OllamaBackend(url, num_ctx=16384)
    for _ in range(2):
        fast.direct("fast-model", "system", "user")
        chat.chat("chat-model", [])
    for model, context in [("fast-model", 8192), ("chat-model", 16384)]:
        assert {r["options"]["num_ctx"] for r in recorded if r["model"] == model} == {context}


def test_timeout_defaults_are_separate(tmp_path, monkeypatch):
    cfg, _ = settings(tmp_path, monkeypatch, {})
    assert cfg.llm_routing_timeout_sec == 8
    assert cfg.llm_chat_timeout_sec == 45
    assert cfg.llm_tools_timeout_sec == 300
    assert cfg.llm_num_ctx == 8192


def test_judge_warmup_uses_routing_budget(ollama_server, tmp_path, monkeypatch):
    from jarvis.listening.intent_judge import IntentJudge, IntentJudgeConfig
    import requests

    url, _ = ollama_server
    cfg, _ = settings(tmp_path, monkeypatch, {"ollama_base_url": url,
        "llm_routing_timeout_sec": 11, "llm_tools_timeout_sec": 120})
    original_post = requests.post
    budgets = []

    def post(*args, **kwargs):
        budgets.append(kwargs["timeout"])
        return original_post(*args, **kwargs)

    monkeypatch.setattr(requests, "post", post)
    judge = IntentJudge(IntentJudgeConfig(cfg=cfg, model="shared"))
    assert judge.warm_up()
    assert 10 < budgets[0] <= cfg.llm_routing_timeout_sec


def test_tool_execution_keeps_its_own_timeout(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from jarvis.tools.builtin import time_tool

    cfg, _ = settings(tmp_path, monkeypatch, {
        "llm_routing_timeout_sec": 2, "llm_tools_timeout_sec": 120,
    })
    budgets = []

    def geocode(location, timeout):
        budgets.append(timeout)
        return "Europe/London", location

    monkeypatch.setattr(time_tool, "_geocode_timezone", geocode)
    context = SimpleNamespace(cfg=cfg, user_print=lambda *args: None)
    result = time_tool.TimeTool().run({"location": "London"}, context)
    assert result.success
    assert budgets == [cfg.llm_tools_timeout_sec]


@pytest.mark.parametrize("explicit_routing", [None, 12.0])
def test_timeout_migration_preserves_explicit_overrides(tmp_path, monkeypatch, explicit_routing):
    overrides = {"llm_tools_timeout_sec": 90, "llm_chat_timeout_sec": 75, "llm_num_ctx": 16384,
                 "unknown_setting": {"keep": True}}
    if explicit_routing is not None:
        overrides["llm_routing_timeout_sec"] = explicit_routing
    cfg, path = settings(tmp_path, monkeypatch, overrides)
    assert cfg.llm_routing_timeout_sec == (explicit_routing or overrides["llm_tools_timeout_sec"])
    assert cfg.llm_tools_timeout_sec == overrides["llm_tools_timeout_sec"]
    assert cfg.llm_chat_timeout_sec == overrides["llm_chat_timeout_sec"]
    assert cfg.llm_num_ctx == overrides["llm_num_ctx"]
    assert json.loads(path.read_text())["unknown_setting"] == overrides["unknown_setting"]
    assert load_settings() == cfg
