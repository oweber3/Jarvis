"""Which local model makes the best tool model: the tool-phase prompt alone, against Ollama directly.

Each case of the everyday, website and PDF sets in ``codex_runner.py`` goes to Ollama's native chat API with
the tool-model prompt (``reply/tool_stage.py``), the real builtin catalogue and Jarvis's context size, at
temperature 0. Tool results come from the case's inert scripts (up to three turns), and the case's own check
scores the calls. Nothing on the PC is touched. Results are recorded in ``docs/TOOL_MODEL_BENCHMARK.md``.

    set PYTHONPATH=src;evals
    python evals/tool_model_bench.py gpt-oss:20b,granite4.2:8b,granite4.2:8b@think

A ``@think`` suffix turns the model's reasoning on.
"""
from __future__ import annotations

import json
import sys
import time

import requests

import codex_runner as c
from jarvis.config import load_settings
from jarvis.reply.tool_stage import TOOL_STAGE_PROMPT
from jarvis.tools.types import ToolExecutionResult

OLLAMA = "http://localhost:11434/api/chat"
CASE_SETS = ("everyday", "website", "pdf")


def tools_for_ollama(cfg) -> list:
    """The builtin catalogue in Ollama's tool format (the DevTools fixture is left out)."""
    return [{"type": "function", "function": {"name": name, "description": spec["description"],
                                              "parameters": spec["inputSchema"]}}
            for name, spec in c.tool_snapshot(cfg).items() if not name.startswith(c.DEVTOOLS_PREFIX)]


def run_case(model: str, case: c.Case, tools: list, num_ctx: int):
    model, _, think = model.partition("@")
    inert = c.InertTools(case.scripts)
    messages = [{"role": "system", "content": TOOL_STAGE_PROMPT}, {"role": "user", "content": case.text}]
    started, first_tool, text = time.time(), None, ""
    for _ in range(3):
        response = requests.post(OLLAMA, json={
            "model": model, "messages": messages, "tools": tools, "stream": False, "think": think == "think",
            "options": {"temperature": 0, "num_ctx": num_ctx}, "keep_alive": "10m"}, timeout=240).json()
        if "error" in response:
            raise RuntimeError(response["error"])
        message = response.get("message", {})
        calls = message.get("tool_calls") or []
        if not calls:
            text = message.get("content") or ""
            break
        messages.append(message)
        for call in calls:
            function = call.get("function", {})
            args = function.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if first_tool is None:
                first_tool = time.time() - started
            result = inert(None, None, function.get("name"), args)
            body = result.reply_text if isinstance(result, ToolExecutionResult) else str(result)
            messages.append({"role": "tool", "content": body or (result.error_message or ""),
                             "tool_name": function.get("name")})
    run = c.Run(kind="question" if "?" in text else "reply", text=text or "ok", seconds=time.time() - started,
                t_first_tool=first_tool, calls=list(inert.calls))
    return run, case.check(run)


def main(models: list[str]) -> dict:
    cfg = load_settings()
    tools = tools_for_ollama(cfg)
    cases = [case for case in c.CASES if case.name.startswith(CASE_SETS)]
    summary = {}
    for model in models:
        passed, first_tools, failures = 0, [], []
        for case in cases:
            try:
                run, problem = run_case(model, case, tools, cfg.llm_num_ctx)
            except Exception as exc:  # noqa: BLE001 - a failing request is a failed case, reported
                run, problem = None, f"error {type(exc).__name__}: {exc}"
            if problem is None:
                passed += 1
            else:
                failures.append((case.name, problem[:140]))
            if run is not None and run.t_first_tool is not None:
                first_tools.append(run.t_first_tool)
        first_tools.sort()
        summary[model] = {"passed": f"{passed}/{len(cases)}", "first_tool_median": round(
            first_tools[len(first_tools) // 2], 2) if first_tools else None}
        print(json.dumps({model: summary[model]}), flush=True)
        for name, problem in failures:
            print(f"   FAIL {name}: {problem}", flush=True)
    return summary


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv[1].split(","))
