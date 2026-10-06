"""📊 Benchmark Jarvis's local models: per-stage latency (cold and warm), VRAM and RAM, and Whisper.

Runs the real intent judge, tool router, planner and a chat reply with synthetic input against the
Ollama server in your Jarvis configuration (``JARVIS_CONFIG_PATH`` or the default config file).
By default it measures the configured fast and chat models; ``--candidates`` adds installed models
to compare. Models that are not installed are skipped, never pulled. No tools run and no user
data is read beyond the model settings.

``--cold`` unloads each model before its first stage (Ollama ``keep_alive: 0``). A running Jarvis
using that model reloads it on its next request. ``--whisper`` also loads the configured Whisper
model and transcribes a phrase spoken by the local Piper voice (no audio is played).

Example:
    PYTHONPATH=src .mamba_env/python.exe scripts/benchmark_models.py --cold --whisper \\
        --candidates qwen3.5:4b --output .tmp/benchmark.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import requests  # noqa: E402

STAGES = ("judge", "router", "planner", "reply")
_QUERY = "what time is it in Tokyo"
_REPLY_QUESTION = "In one sentence, why is the sky blue?"
_WHISPER_PHRASE = "Jarvis, open Word and set the volume to thirty percent."


def _say(line: str) -> None:
    print(line, flush=True)


class _Recorder:
    """Captures Ollama's own timings for every ``/api/chat`` call made inside a stage."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._original = requests.post

    def __enter__(self) -> "_Recorder":
        def record(*args: Any, **kwargs: Any) -> Any:
            response = self._original(*args, **kwargs)
            if str(args[0] if args else kwargs.get("url", "")).endswith("/api/chat"):
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                self.calls.append({
                    "load_seconds": round((data.get("load_duration") or 0) / 1e9, 3),
                    "prompt_tokens": data.get("prompt_eval_count"),
                    "output_tokens": data.get("eval_count"),
                    "generation_seconds": round((data.get("eval_duration") or 0) / 1e9, 3),
                })
            return response

        requests.post = record
        return self

    def __exit__(self, *exc: Any) -> None:
        requests.post = self._original


def _timed(run: Callable[[], Any]) -> Dict[str, Any]:
    with _Recorder() as recorder:
        start = time.perf_counter()
        try:
            run()
            error = None
        except Exception as exc:  # a failed stage is reported, not fatal
            error = type(exc).__name__
        seconds = time.perf_counter() - start
    result: Dict[str, Any] = {"seconds": round(seconds, 3), "llm_calls": len(recorder.calls),
                              "load_seconds": round(sum(c["load_seconds"] for c in recorder.calls), 3),
                              "output_tokens": sum(c["output_tokens"] or 0 for c in recorder.calls)}
    if error:
        result["error"] = error
    return result


def _stage_runner(cfg: Any, model: str) -> Dict[str, Callable[[], Any]]:
    from jarvis.listening.intent_judge import IntentJudge, IntentJudgeConfig
    from jarvis.listening.transcript_buffer import TranscriptSegment
    from jarvis.llm import get_llm_backend
    from jarvis.reply.planner import plan_query
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    stage_cfg = dataclasses.replace(cfg, fast_model=model, llm_chat_model=model)
    backend = get_llm_backend(stage_cfg)
    system_prompt = build_system_prompt()
    tools = [(name, tool.description) for name, tool in BUILTIN_TOOLS.items()]

    def judge() -> Any:
        now = time.time()
        return IntentJudge(IntentJudgeConfig(cfg=stage_cfg, model=model)).judge(
            [TranscriptSegment(f"Jarvis {_QUERY}", now - 2.0, now - 0.5)], wake_timestamp=now - 1.8)

    def router() -> Any:
        return select_tools(query=_QUERY, builtin_tools=BUILTIN_TOOLS, mcp_tools={},
                            strategy=ToolSelectionStrategy.LLM, llm_backend=backend, llm_model=model,
                            llm_timeout_sec=stage_cfg.llm_routing_timeout_sec)

    def planner() -> Any:
        return plan_query(cfg=stage_cfg, query=_QUERY, dialogue_context="", tools=tools)

    def reply() -> Any:
        return backend.chat(model, [{"role": "system", "content": system_prompt},
                                    {"role": "user", "content": _REPLY_QUESTION}],
                            timeout_sec=stage_cfg.llm_chat_timeout_sec, extra_options={"max_tokens": 120})

    return {"judge": judge, "router": router, "planner": planner, "reply": reply}


def _installed(url: str) -> List[str]:
    response = requests.get(f"{url}/api/tags", timeout=10)
    response.raise_for_status()
    return [m.get("name", "") for m in response.json().get("models", [])]


def _loaded(url: str) -> Dict[str, Dict[str, Any]]:
    try:
        response = requests.get(f"{url}/api/ps", timeout=10)
        response.raise_for_status()
        return {m.get("name", ""): m for m in response.json().get("models", [])}
    except (requests.RequestException, ValueError):
        return {}


def _unload(url: str, model: str) -> None:
    requests.post(f"{url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=60).raise_for_status()


def _gpu_used_bytes() -> Optional[int]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
        return int(sum(float(v) for v in out.split()) * 1024 * 1024)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _summary(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    seconds = [r["seconds"] for r in runs if "error" not in r]
    if not seconds:
        return {"median_seconds": None, "errors": len(runs)}
    return {"median_seconds": round(statistics.median(seconds), 3), "max_seconds": max(seconds),
            "errors": len(runs) - len(seconds)}


def _benchmark_model(cfg: Any, url: str, model: str, roles: List[str], repeats: int,
                     cold: bool) -> Dict[str, Any]:
    _say(f"🧠 {model} ({', '.join(roles)})")
    runners = _stage_runner(cfg, model)
    entry: Dict[str, Any] = {"model": model, "roles": roles, "stages": {}}
    if cold:
        _unload(url, model)
        first = STAGES[0]
        entry["cold"] = {"stage": first, **_timed(runners[first])}
        _say(f"  🧊 Cold {first}: {entry['cold']['seconds']:.2f} s (load {entry['cold']['load_seconds']:.2f} s)")
    else:
        runners[STAGES[0]]()  # warm the model so every timed run below is a warm run
    for stage in STAGES:
        warm = [_timed(runners[stage]) for _ in range(repeats)]
        entry["stages"][stage] = {"warm": warm, **_summary(warm)}
        median = entry["stages"][stage]["median_seconds"]
        shown = "failed" if median is None else f"{median:.2f} s median"
        _say(f"  ⏱️ {stage}: {shown} over {repeats} warm run(s)")
    loaded = _loaded(url).get(model, {})
    size, vram = int(loaded.get("size") or 0), int(loaded.get("size_vram") or 0)
    entry["memory"] = {"vram_bytes": vram, "ram_bytes": max(0, size - vram),
                       "context_length": loaded.get("context_length")}
    _say(f"  💾 VRAM {vram / 1e9:.2f} GB, RAM {max(0, size - vram) / 1e9:.2f} GB")
    return entry


def _speech_wav(path: Path) -> Optional[Path]:
    """Speak the test phrase with the local Piper voice into ``path``. Nothing is played."""
    try:
        from piper.voice import PiperVoice
        from jarvis.output.tts import _get_default_piper_model_path
    except ImportError:
        return None
    model_path = Path(_get_default_piper_model_path())
    if not model_path.is_file():
        return None
    voice = PiperVoice.load(str(model_path))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(voice.config.sample_rate)
        for chunk in voice.synthesize(_WHISPER_PHRASE):
            wav.writeframes(chunk.audio_int16_bytes)
    return path


def _benchmark_whisper(cfg: Any, repeats: int, wav_path: Optional[Path]) -> Dict[str, Any]:
    _say(f"🎤 Whisper {cfg.whisper_model} ({cfg.whisper_device}, {cfg.whisper_compute_type})")
    from faster_whisper import WhisperModel

    with tempfile.TemporaryDirectory() as tmp:
        audio = wav_path or _speech_wav(Path(tmp) / "phrase.wav")
        if audio is None:
            _say("  ⚠️ No Piper voice and no --wav file; Whisper skipped")
            return {"skipped": "no_audio"}
        with wave.open(str(audio), "rb") as wav:
            duration = wav.getnframes() / float(wav.getframerate())
        gpu_before = _gpu_used_bytes()
        start = time.perf_counter()
        model = WhisperModel(cfg.whisper_model, device=cfg.whisper_device, compute_type=cfg.whisper_compute_type)
        load_seconds = time.perf_counter() - start
        gpu_after = _gpu_used_bytes()

        def transcribe() -> str:
            segments, _info = model.transcribe(str(audio), vad_filter=cfg.whisper_vad)
            return " ".join(s.text for s in segments)

        first = _timed(transcribe)
        warm = [_timed(transcribe) for _ in range(repeats)]
    result = {"model": cfg.whisper_model, "device": cfg.whisper_device, "compute_type": cfg.whisper_compute_type,
              "audio_seconds": round(duration, 2), "load_seconds": round(load_seconds, 3),
              "vram_bytes": (gpu_after - gpu_before) if gpu_before is not None and gpu_after is not None else None,
              "first": first, "warm": warm, **_summary(warm)}
    _say(f"  ⏳ Load {load_seconds:.2f} s, first transcription {first['seconds']:.2f} s, "
         f"warm median {result['median_seconds']} s for {duration:.1f} s of audio")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", help="Benchmark exactly these models instead of the configured ones")
    parser.add_argument("--candidates", nargs="+", default=[], help="Installed models to compare")
    parser.add_argument("--repeats", type=int, default=3, help="Warm runs per stage")
    parser.add_argument("--cold", action="store_true", help="Unload each model before its first stage")
    parser.add_argument("--whisper", action="store_true", help="Also benchmark the configured Whisper model")
    parser.add_argument("--wav", type=Path, help="Audio for the Whisper benchmark instead of Piper speech")
    parser.add_argument("--quiet-pc", action="store_true", help="Label the run as measured while the PC was idle")
    parser.add_argument("--output", type=Path, help="Write the JSON report here")
    args = parser.parse_args(argv)

    from jarvis.config import load_settings
    from jarvis.llm import Tier, resolve_model

    cfg = load_settings()
    if cfg.llm_provider != "ollama":
        _say("❌ The benchmark measures Ollama; the configured provider is not Ollama")
        return 2
    url = cfg.ollama_base_url.rstrip("/")
    roles: Dict[str, List[str]] = {}
    if args.models:
        for model in args.models:
            roles.setdefault(model, []).append("requested")
    else:
        roles.setdefault(resolve_model(cfg, Tier.FAST), []).append("fast")
        roles.setdefault(resolve_model(cfg, Tier.CHAT), []).append("chat")
    for model in args.candidates:
        roles.setdefault(model, []).append("candidate")

    in_use = not args.quiet_pc
    _say("📊 Jarvis model benchmark" + (" (measured while the PC was in use)" if in_use else ""))
    _say(f"  🔌 Ollama {url}, num_ctx {cfg.llm_num_ctx}, keep_alive {cfg.llm_keep_alive}")
    installed = set(_installed(url))
    report: Dict[str, Any] = {"measured_while_in_use": in_use, "num_ctx": cfg.llm_num_ctx,
                              "keep_alive": cfg.llm_keep_alive, "repeats": args.repeats,
                              "cold": args.cold, "models": [], "skipped": []}
    for model, model_roles in roles.items():
        if model not in installed:
            _say(f"⏭️ {model}: not installed, skipped (the benchmark never downloads models)")
            report["skipped"].append({"model": model, "reason": "not_installed"})
            continue
        report["models"].append(_benchmark_model(cfg, url, model, model_roles, args.repeats, args.cold))
    if args.whisper:
        report["whisper"] = _benchmark_whisper(cfg, args.repeats, args.wav)
    report["gpu_used_bytes"] = _gpu_used_bytes()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _say(f"💾 Report written to {args.output}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
