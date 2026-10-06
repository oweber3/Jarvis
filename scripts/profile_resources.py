#!/usr/bin/env python3
"""Measure Jarvis's start-up import time, idle CPU and memory, and reply-engine overhead.

Everything outside Jarvis is faked so the numbers describe Jarvis's own work and can be taken on
any machine (a development laptop, CI, a Linux container):

- audio: a fake ``sounddevice`` delivers silent 20 ms blocks in real time to the listener's callback
- Whisper: ``faster_whisper.WhisperModel`` is replaced by a stand-in (the real package is still
  imported, so its import cost is counted, but no model is loaded)
- Ollama: a local HTTP server answers ``/api/version``, ``/api/tags``, ``/api/chat`` and
  ``/api/embeddings`` at once with fixed replies
- TTS, location and dictation are off; Windows tools are absent unless run on Windows

Numbers that depend on the PC (Whisper on CUDA, real model residency, VRAM) are not measured here.

    PYTHONPATH=src .mamba_env/python.exe scripts/profile_resources.py
    PYTHONPATH=src python scripts/profile_resources.py --idle-seconds 60 --requests 40 --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FAKE_MODELS = ("fake-chat", "fake-fast", "nomic-embed-text")


# ── Fake Ollama ──────────────────────────────────────────────────────────────

class _OllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _reply(self, data, stream=False):
        body = (json.dumps(data) + ("\n" if stream else "")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._reply({"models": [{"name": name, "model": name} for name in FAKE_MODELS]})
        else:
            self._reply({"version": "0.12.0"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if self.path.startswith("/api/embed"):
            self._reply({"embedding": [0.01] * 768, "embeddings": [[0.01] * 768]})
            return
        self._reply({"model": body.get("model", ""), "message": {"role": "assistant", "content": "ok"},
                     "done": True}, stream=bool(body.get("stream")))


def start_fake_ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="fake-ollama").start()
    return server, f"http://127.0.0.1:{server.server_port}"


def write_config(directory: Path, ollama_url: str) -> Path:
    path = directory / "config.json"
    path.write_text(json.dumps({
        "ollama_base_url": ollama_url, "llm_chat_model": "fake-chat", "fast_model": "fake-fast",
        "embedding_model": "nomic-embed-text", "db_path": str(directory / "jarvis.db"),
        "tts_enabled": False, "location_enabled": False, "dictation_enabled": False,
        "whisper_model": "tiny", "whisper_device": "cpu",
    }), encoding="utf-8")
    return path


# ── Fakes installed in the child process ──────────────────────────────────────

def _fake_sounddevice() -> ModuleType:
    import numpy as np

    sd = ModuleType("sounddevice")

    class CallbackStop(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    class PortAudioError(Exception):
        pass

    devices = [{"name": "Fake microphone", "max_input_channels": 1, "max_output_channels": 0,
                "default_samplerate": 16000.0}]

    def query_devices(device=None, kind=None):
        return devices[0] if device is not None or kind is not None else list(devices)

    class InputStream:
        """Calls ``callback`` with silent blocks at the real-time rate, like PortAudio."""

        def __init__(self, samplerate=16000, channels=1, dtype="float32", blocksize=320, callback=None, **_kw):
            self.samplerate, self.channels, self.blocksize, self.callback = samplerate, channels, blocksize, callback
            self.active = False
            self._thread = None

        def _pump(self):
            period = self.blocksize / float(self.samplerate)
            block = np.zeros((self.blocksize, self.channels), dtype=np.float32)
            next_at = time.monotonic()
            while self.active:
                if self.callback is not None:
                    self.callback(block, self.blocksize, None, None)
                next_at += period
                time.sleep(max(0.0, next_at - time.monotonic()))

        def start(self):
            if not self.active:
                self.active = True
                self._thread = threading.Thread(target=self._pump, daemon=True, name="fake-portaudio")
                self._thread.start()

        def stop(self):
            self.active = False

        def close(self):
            self.active = False

        def read(self, frames):
            return np.zeros((frames, self.channels), dtype=np.float32), False

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, *_exc):
            self.close()

    sd.CallbackStop, sd.CallbackAbort, sd.PortAudioError = CallbackStop, CallbackAbort, PortAudioError
    sd.query_devices, sd.InputStream, sd.OutputStream = query_devices, InputStream, InputStream
    sd.default = SimpleNamespace(device=(0, 0), samplerate=16000)
    sd.play = sd.wait = lambda *a, **k: None
    sd.rec = lambda frames, **k: np.zeros((int(frames), 1), dtype=np.float32)
    return sd


class _FakeWhisperModel:
    def __init__(self, *_a, **_k):
        pass

    def transcribe(self, *_a, **_k):
        return iter(()), SimpleNamespace(language="en", language_probability=1.0, duration=0.0)


def install_fakes() -> None:
    sys.modules["sounddevice"] = _fake_sounddevice()
    import faster_whisper  # the real package: its import cost is part of start-up
    faster_whisper.WhisperModel = _FakeWhisperModel


def child_daemon() -> None:
    install_fakes()
    sys.argv = [sys.argv[0]]
    from jarvis import daemon
    daemon.main()


# ── Measurements ──────────────────────────────────────────────────────────────

_IMPORT_LINE = re.compile(r"import time:\s+(\d+)\s+\|\s+(\d+)\s+\|(\s*)(\S+)")


def measure_imports(module: str, runs: int) -> dict:
    """Cumulative import time of ``module`` (best of ``runs``) and the slowest top-level imports."""
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "0"}
    best = None
    for _ in range(runs):
        started = time.perf_counter()
        proc = subprocess.run([sys.executable, "-X", "importtime", "-c", f"import {module}"],
                              capture_output=True, text=True, env=env, cwd=str(ROOT))
        wall = time.perf_counter() - started
        rows = [m for m in (_IMPORT_LINE.search(line) for line in proc.stderr.splitlines()) if m]
        total = next((int(m.group(2)) for m in rows if m.group(4) == module and not m.group(3).strip(" ")), None)
        if total is None:
            total = next((int(m.group(2)) for m in reversed(rows) if m.group(4) == module), 0)
        result = {"cumulative_ms": total / 1000.0, "process_wall_ms": wall * 1000.0, "rows": rows}
        if best is None or result["cumulative_ms"] < best["cumulative_ms"]:
            best = result
    # Heaviest packages imported along the way, by their own cumulative time (top-level names only).
    heaviest: dict = {}
    for m in best.pop("rows"):
        name = m.group(4)
        if "." in name:
            continue
        heaviest[name] = max(heaviest.get(name, 0), int(m.group(2)))
    top = sorted(heaviest.items(), key=lambda kv: kv[1], reverse=True)[:12]
    best["heaviest_ms"] = {name: us / 1000.0 for name, us in top if name != module}
    return best


def measure_idle(idle_seconds: float, settle_seconds: float) -> dict:
    import psutil
    server, url = start_fake_ollama()
    with tempfile.TemporaryDirectory() as tmp:
        config = write_config(Path(tmp), url)
        env = {**os.environ, "PYTHONPATH": str(SRC), "JARVIS_CONFIG_PATH": str(config),
               "PYTHONUNBUFFERED": "1", "JARVIS_PROFILE_CHILD": "1"}
        started = time.perf_counter()
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child-daemon"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                cwd=str(ROOT))
        lines: list = []
        ready = threading.Event()

        def read():
            for line in proc.stdout:
                lines.append(line.rstrip())
                if "Listening!" in line:
                    ready.set()

        threading.Thread(target=read, daemon=True).start()
        try:
            if not ready.wait(120):
                raise RuntimeError("daemon did not reach Listening!:\n" + "\n".join(lines[-40:]))
            to_listening = time.perf_counter() - started
            time.sleep(settle_seconds)
            process = psutil.Process(proc.pid)
            cpu0, wall0 = process.cpu_times(), time.perf_counter()
            rss_samples = []
            end = wall0 + idle_seconds
            while time.perf_counter() < end:
                rss_samples.append(process.memory_info().rss)
                time.sleep(min(1.0, max(0.0, end - time.perf_counter())))
            cpu1, wall1 = process.cpu_times(), time.perf_counter()
            busy = (cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)
            return {
                "seconds_to_listening": to_listening,
                "idle_window_s": wall1 - wall0,
                "idle_cpu_percent": 100.0 * busy / (wall1 - wall0),
                "idle_cpu_seconds": busy,
                "rss_mb": statistics.median(rss_samples) / 2**20,
                "rss_peak_mb": max(rss_samples) / 2**20,
                "threads": process.num_threads(),
                "context_switches": process.num_ctx_switches().voluntary,
            }
        finally:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()


def measure_requests(requests: int) -> dict:
    """Wall time per reply-engine request against the instant fake LLM (Jarvis's own overhead)."""
    server, url = start_fake_ollama()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["JARVIS_CONFIG_PATH"] = str(write_config(Path(tmp), url))
        sys.path.insert(0, str(SRC))
        import io
        import contextlib
        from jarvis.config import load_settings
        from jarvis.memory.conversation import DialogueMemory
        from jarvis.memory.db import Database
        from jarvis.reply.engine import run_reply_engine
        cfg = load_settings()
        db = Database(cfg.db_path, cfg.sqlite_vss_path)
        memory = DialogueMemory(inactivity_timeout=300, max_interactions=20)
        results = {}
        # Each LLM-route request is new text, so the router and extractor hot caches do not hide work.
        for label, text in (("llm_route", "tell me something interesting about owls, part {i}"),
                            ("fast_route", "what time is it")):
            timings = []
            for i in range(requests + 2):
                sink = io.StringIO()
                started = time.perf_counter()
                with contextlib.redirect_stdout(sink):
                    run_reply_engine(db, cfg, None, text.format(i=i), memory, quiet=True)
                elapsed = time.perf_counter() - started
                if i >= 2:  # the first calls warm caches and imports
                    timings.append(elapsed * 1000.0)
            results[label] = {"median_ms": statistics.median(timings), "p90_ms": sorted(timings)[int(0.9 * len(timings)) - 1],
                              "requests": len(timings)}
        import psutil
        results["rss_after_mb"] = psutil.Process().memory_info().rss / 2**20
        server.shutdown()
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--idle-seconds", type=float, default=60.0)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--import-runs", type=int, default=5)
    parser.add_argument("--skip", action="append", default=[], choices=["imports", "idle", "requests"])
    parser.add_argument("--json", type=Path)
    parser.add_argument("--child-daemon", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_daemon:
        child_daemon()
        return 0

    report: dict = {"python": sys.version.split()[0], "platform": sys.platform}
    print("📊 Jarvis resource profile (audio, Whisper and Ollama faked)")
    if "imports" not in args.skip:
        print("  ⏱️ Start-up imports (python -X importtime, best of %d)" % args.import_runs)
        for module in ("jarvis.daemon", "jarvis.reply.engine", "desktop_app.app"):
            result = measure_imports(module, args.import_runs)
            report.setdefault("imports", {})[module] = result
            print(f"    📦 {module}: {result['cumulative_ms']:.0f} ms cumulative, "
                  f"{result['process_wall_ms']:.0f} ms process wall")
            for name, ms in list(result["heaviest_ms"].items())[:6]:
                print(f"        • {name}: {ms:.0f} ms")
    if "idle" not in args.skip:
        print(f"  💤 Daemon idle for {args.idle_seconds:.0f} s after reaching Listening!")
        idle = measure_idle(args.idle_seconds, args.settle_seconds)
        report["idle"] = idle
        print(f"    🚀 Start to Listening!: {idle['seconds_to_listening']:.1f} s")
        print(f"    🔥 Idle CPU: {idle['idle_cpu_percent']:.2f}% of one core "
              f"({idle['idle_cpu_seconds']:.2f} s over {idle['idle_window_s']:.0f} s)")
        print(f"    🧠 Resident memory: {idle['rss_mb']:.0f} MB median, {idle['rss_peak_mb']:.0f} MB peak")
        print(f"    🧵 Threads: {idle['threads']}")
    if "requests" not in args.skip:
        print(f"  💬 Reply engine overhead, {args.requests} requests per route, instant fake LLM")
        req = measure_requests(args.requests)
        report["requests"] = req
        for label in ("llm_route", "fast_route"):
            print(f"    ⚙️ {label}: {req[label]['median_ms']:.1f} ms median, {req[label]['p90_ms']:.1f} ms p90")
        print(f"    🧠 Resident memory after the requests: {req['rss_after_mb']:.0f} MB")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"  💾 Saved {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
