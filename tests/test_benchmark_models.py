"""The model benchmark measures every stage per model against an Ollama server, never pulls models."""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location("benchmark_models", ROOT / "scripts" / "benchmark_models.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_ollama():
    calls = []
    installed = ["fast:1b", "chat:9b", "candidate:4b"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(("GET", self.path, None))
            if self.path == "/api/tags":
                self.reply({"models": [{"name": name} for name in installed]})
            elif self.path == "/api/ps":
                loaded = {body["model"] for method, _, body in calls
                          if method == "POST" and body and body.get("keep_alive") != 0}
                self.reply({"models": [{"name": m, "size": 3_000_000_000, "size_vram": 2_500_000_000,
                                        "context_length": 8192} for m in sorted(loaded)]})
            else:
                self.reply({"version": "test"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(("POST", self.path, body))
            self.reply({"message": {"role": "assistant", "content": "OK"}, "done": True,
                        "load_duration": 1_000_000, "prompt_eval_count": 50, "eval_count": 3,
                        "eval_duration": 30_000_000})

        def reply(self, data):
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def scratch_config(tmp_path, monkeypatch):
    def write(url):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"_config_version": 5, "ollama_base_url": url, "ollama_chat_model": "chat:9b",
                                    "fast_model": "fast:1b", "db_path": str(tmp_path / "jarvis.db")}))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    return write


def test_reports_every_stage_for_configured_and_candidate_models(fake_ollama, scratch_config, tmp_path):
    url, _ = fake_ollama
    scratch_config(url)
    output = tmp_path / "report.json"
    benchmark = _load_script()
    assert benchmark.main(["--candidates", "candidate:4b", "--repeats", "2", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert [m["model"] for m in report["models"]] == ["fast:1b", "chat:9b", "candidate:4b"]
    assert [m["roles"] for m in report["models"]] == [["fast"], ["chat"], ["candidate"]]
    for entry in report["models"]:
        assert set(entry["stages"]) == set(benchmark.STAGES)
        for runs in entry["stages"].values():
            assert len(runs["warm"]) == 2
            assert all(run["seconds"] >= 0 for run in runs["warm"])
        assert entry["memory"]["vram_bytes"] == 2_500_000_000
        assert entry["memory"]["ram_bytes"] == 500_000_000
    assert report["measured_while_in_use"] is True


def test_cold_runs_unload_the_model_first_and_missing_models_are_never_pulled(fake_ollama, scratch_config, tmp_path):
    url, calls = fake_ollama
    scratch_config(url)
    output = tmp_path / "report.json"
    benchmark = _load_script()
    assert benchmark.main(["--models", "chat:9b", "absent:70b", "--cold", "--repeats", "1",
                           "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert [m["model"] for m in report["models"]] == ["chat:9b"]
    assert report["skipped"] == [{"model": "absent:70b", "reason": "not_installed"}]
    unloads = [body for method, path, body in calls if method == "POST" and body.get("keep_alive") == 0]
    assert [body["model"] for body in unloads] == ["chat:9b"]
    cold = report["models"][0]["cold"]
    assert cold["stage"] == benchmark.STAGES[0] and cold["seconds"] >= 0
    assert not any(path.startswith("/api/pull") for _, path, _ in calls)
