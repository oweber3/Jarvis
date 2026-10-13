"""Personality routing: requests to change how Jarvis talks reach ``personality``; its neighbours do not.

Runs the local router (fast model) and chat call (model under test) with the whole built-in catalogue,
``replyMode`` included. Nothing is executed. Phrasings here are ones the fast path does not cover, so
the model has to choose. A style instruction that names no personality ("be more brief", "drop the
jokes") is a conversational instruction acknowledged without tools (``prompts.spec.md``, USER
INSTRUCTIONS), so it is not a case here.

    EVAL_JUDGE_MODEL=gpt-oss:20b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_personality_routing.py -m eval -v
"""
import json
import os

import pytest

from conftest import requires_judge_llm
from helpers import JUDGE_MODEL, eval_fast_model

# (utterance, expected personality arguments, or None when personality must not be called)
CASES = [
    ("Change your personality to the friendly one", {"action": "set", "name": "friendly"}),
    ("Could you switch your personality over to plain, please?", {"action": "set", "name": "plain"}),
    ("Go back to being the witty butler", {"action": "set", "name": "butler"}),
    ("Use Claude", None),
    ("Write a friendly birthday message for my sister", None),
    ("Open Word", None),
]


@pytest.fixture(autouse=True)
def reply_mode_offered():
    from jarvis.tools.builtin.reply_mode import ReplyModeTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS["replyMode"] = ReplyModeTool()
    yield
    BUILTIN_TOOLS.pop("replyMode", None)


def _ask(query):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    backend = OllamaBackend(os.environ.get("EVAL_OLLAMA_URL", "http://127.0.0.1:11434"))
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=eval_fast_model(), llm_timeout_sec=60)
    response = backend.chat(JUDGE_MODEL, [
        {"role": "system", "content": build_system_prompt() + "\n" + generate_tools_description(selected)},
        {"role": "user", "content": query}], timeout_sec=120,
        extra_options={"temperature": 0, "num_predict": 2048}, tools=generate_tools_json_schema(selected))
    assert response, "No model response"
    return selected, response.get("message", {}).get("tool_calls", []) or []


@pytest.mark.eval
@requires_judge_llm
@pytest.mark.parametrize("query,expected", CASES, ids=[c[0] for c in CASES])
def test_personality_requests_reach_the_personality_tool(query, expected):
    selected, calls = _ask(query)
    names = [call["function"]["name"] for call in calls]
    print(f"\n🎭 {query}\n   🧭 router: {selected}\n   🔧 calls: {names}")
    if expected is None:
        assert "personality" not in names, f"{query}: called personality ({calls})"
        return
    assert "personality" in selected, f"{query}: router selected {selected}"
    assert names and names[0] == "personality", f"{query}: called {names}"
    args = calls[0]["function"]["arguments"]
    args = json.loads(args) if isinstance(args, str) else args
    for key, value in expected.items():
        assert str(args.get(key, "")).casefold() == value, f"{query}: {args}"
