"""Local, synthetic Ollama request-shape probe (no user data or tool execution)."""

import argparse
import json
import sys
import time
from types import SimpleNamespace

import requests

from jarvis.llm import get_llm_backend


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3.5:0.8b")
    parser.add_argument("--url", default="http://127.0.0.1:11434")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--cold", action="store_true", help="Unload the probe model before measuring its first call")
    parser.add_argument("--stages", action="store_true", help="Run the real judge, router and planner with synthetic input")
    args = parser.parse_args()
    cfg = SimpleNamespace(ollama_base_url=args.url, llm_num_ctx=8192, low_power_mode=False,
                          fast_model=args.model, llm_chat_model=args.model,
                          llm_routing_timeout_sec=8, planner_timeout_sec=3)
    backend = get_llm_backend(cfg)
    original_post = requests.post
    if args.cold:
        original_post(args.url + "/api/generate", json={"model": args.model, "keep_alive": 0}, timeout=60).raise_for_status()
    records = []
    stage = ""

    def record_post(*pos, **kwargs):
        start = time.perf_counter()
        response = original_post(*pos, **kwargs)
        if pos[0].endswith("/api/chat"):
            data = response.json()
            record = {
                "stage": stage, "seconds": round(time.perf_counter() - start, 3),
                "num_ctx": kwargs["json"].get("options", {}).get("num_ctx"),
                "keep_alive": kwargs["json"].get("keep_alive"),
                "load_seconds": round(data.get("load_duration", 0) / 1e9, 3),
                "output_tokens": data.get("eval_count"),
            }
            records.append(record)
            print("⏱️ " + json.dumps(record), flush=True)
        return response

    requests.post = record_post
    if args.stages:
        from jarvis.listening.intent_judge import IntentJudge, IntentJudgeConfig
        from jarvis.listening.transcript_buffer import TranscriptSegment
        from jarvis.reply.planner import plan_query
        from jarvis.tools.registry import BUILTIN_TOOLS
        from jarvis.tools.selection import select_tools, ToolSelectionStrategy

        stage = "cold:warmup" if args.cold else "warmup"
        assert backend.warm_up(args.model, timeout_sec=cfg.llm_routing_timeout_sec)
        for turn in range(2):
            stage = f"{turn + 1}:judge"
            judge = IntentJudge(IntentJudgeConfig(cfg=cfg, model=args.model, timeout_sec=6))
            result = judge.judge([TranscriptSegment("Jarvis what time is it", 1000.0, 1001.0)], wake_timestamp=1000.5)
            print(f"🧠 Judge: {result}", flush=True)
            stage = f"{turn + 1}:router"
            selected = select_tools(query="what time is it", builtin_tools=BUILTIN_TOOLS, mcp_tools={},
                strategy=ToolSelectionStrategy.LLM, llm_backend=backend, llm_model=args.model,
                llm_timeout_sec=cfg.llm_routing_timeout_sec)
            print(f"🧭 Router: {selected}", flush=True)
            stage = f"{turn + 1}:planner"
            plan = plan_query(cfg=cfg, query="what time is it", dialogue_context="",
                tools=[(name, BUILTIN_TOOLS[name].description) for name in selected if name in BUILTIN_TOOLS])
            print(f"📋 Planner: {plan}", flush=True)
        print("📊 " + json.dumps(records), flush=True)
        return
    system = "Return only the word OK."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "ping"}]
    for turn in range(2):
        for name in (["warmup", "judge", "router", "planner"] if turn == 0 else ["judge", "router", "planner"]):
            stage = f"{turn + 1}:{name}"
            if args.legacy:
                options = {"num_predict": 1 if name == "warmup" else 10, "temperature": 0.0}
                if name != "warmup":
                    options["num_ctx"] = 4096 if name == "router" else 8192
                payload = {"model": args.model, "messages": messages, "stream": False, "options": options}
                if name in {"warmup", "judge"}:
                    payload["keep_alive"] = "30m"
                if name != "warmup":
                    payload["think"] = False
                requests.post(args.url + "/api/chat", json=payload, timeout=60).raise_for_status()
            elif name == "warmup":
                assert backend.warm_up(args.model, timeout_sec=60)
            elif name == "judge":
                assert backend.chat(args.model, messages, timeout_sec=8,
                                    extra_options={"max_tokens": 10, "temperature": 0.0})
            else:
                assert backend.direct(args.model, system, "ping", timeout_sec=8,
                                      max_tokens=10, temperature=0.0)
    print("📊 " + json.dumps(records), flush=True)


if __name__ == "__main__":
    main()
