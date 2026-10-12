"""Calendar and email routing: agenda and inbox questions reach ``calendar`` and ``email``, and their
neighbours (the time and date, the web) keep theirs.

Runs the local model's tool selection and chat call with both tools offered next to the built-in tools.
Nothing is executed and no calendar or mailbox is contacted.

    EVAL_WINDOWS_MODEL=qwen3.5:9b PYTHONPATH=src .mamba_env/python.exe -m pytest evals/test_calendar_email_routing.py -m eval -v

EVAL_OLLAMA_URL selects a server other than the default http://127.0.0.1:11434.
"""
import json
import os

import pytest

# (utterance, tool, expected arguments, or None to check the tool only). An expected value of None means the
# argument may be absent; a tuple lists accepted values. A tool of None means a neighbour: neither calendar nor
# email may be called (the model may answer the time itself or call getTime).
CASES = [
    ("What's on my calendar today?", "calendar", {"action": ("agenda", None), "range": ("today", None)}),
    ("What have I got on tomorrow?", "calendar", {"range": "tomorrow"}),
    ("What's on my schedule this week?", "calendar", {"range": "week"}),
    ("When is my next meeting?", "calendar", {"action": "next"}),
    ("Do I have any lectures on Friday?", "calendar", None),
    ("Any new emails?", "email", {"action": ("list", None)}),
    ("Did I get anything from Sam?", "email", {"from": "sam"}),
    ("Read me the latest email from Professor King", "email", {"action": "read"}),
    ("Check my inbox", "email", {"action": ("list", None)}),
    ("What time is it?", None, None),
    ("What's the date today?", None, None),
    ("Search the web for the World Cup schedule", None, None),
]


@pytest.fixture(autouse=True)
def tools_offered():
    from jarvis.tools.builtin.calendar_tool import CalendarTool
    from jarvis.tools.builtin.email_tool import EmailTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS["calendar"] = CalendarTool()
    BUILTIN_TOOLS["email"] = EmailTool()
    yield
    BUILTIN_TOOLS.pop("calendar", None)
    BUILTIN_TOOLS.pop("email", None)


def _ask(query):
    from jarvis.llm.ollama import OllamaBackend
    from jarvis.system_prompt import build_system_prompt
    from jarvis.tools.registry import BUILTIN_TOOLS, generate_tools_description, generate_tools_json_schema
    from jarvis.tools.selection import ToolSelectionStrategy, select_tools

    backend = OllamaBackend(os.environ.get("EVAL_OLLAMA_URL", "http://127.0.0.1:11434"))
    model = os.environ.get("EVAL_WINDOWS_MODEL", "qwen3.5:0.8b")
    assert model in backend.list_models(), "Install or select a local eval model"
    selected = select_tools(query, BUILTIN_TOOLS, {}, ToolSelectionStrategy.LLM,
                            llm_backend=backend, llm_model=model, llm_timeout_sec=30)
    response = backend.chat(model, [
        {"role": "system", "content": build_system_prompt() + "\n" + generate_tools_description(selected)},
        {"role": "user", "content": query}], timeout_sec=60,
        extra_options={"temperature": 0, "num_predict": 1024}, tools=generate_tools_json_schema(selected))
    assert response, "No model response"
    return selected, response.get("message", {}).get("tool_calls", [])


def _matches(value, expected):
    accepted = expected if isinstance(expected, tuple) else (expected,)
    for option in accepted:
        if option is None and value in (None, ""):
            return True
        if option is not None and option.casefold() in str(value or "").casefold():
            return True
    return False


@pytest.mark.eval
@pytest.mark.parametrize("query,tool,expected", CASES, ids=[c[0] for c in CASES])
def test_calendar_and_email_requests_reach_the_right_tool(query, tool, expected):
    selected, calls = _ask(query)
    if tool is None:
        called = [c["function"]["name"] for c in calls]
        assert not {"calendar", "email"} & set(called), f"{query}: called {called}"
        return
    assert tool in selected, f"{query}: router selected {selected}"
    assert calls, f"{query}: no tool call"
    name = calls[0]["function"]["name"]
    args = calls[0]["function"]["arguments"]
    args = json.loads(args) if isinstance(args, str) else args
    assert name == tool, f"{query}: chose {name} {args}"
    for key, value in (expected or {}).items():
        assert _matches(args.get(key), value), f"{query}: {args}"
