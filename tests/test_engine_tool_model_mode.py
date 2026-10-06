"""Tool-model mode: the tool model chooses and calls tools, the chat model writes the reply
(reply.spec.md, Tool-Model Mode). The LLM and the tools are faked; tests assert which model did what."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis.tools.types import ToolExecutionResult

TOOL_MODEL = "gpt-oss:20b"
CHAT_MODEL = "qwen3.5:9b"


def _text(content):
    return {"message": {"role": "assistant", "content": content}}


def _call(name, arguments, thinking=""):
    message = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}
    if thinking:
        message["thinking"] = thinking
    return {"message": message}


class Harness:
    """Runs one reply with scripted model responses; records model calls and tool runs."""

    def __init__(self, mock_config, db, dialogue_memory, *, tool_model=TOOL_MODEL):
        mock_config.ollama_chat_model = mock_config.llm_chat_model = CHAT_MODEL
        mock_config.tool_model = tool_model
        mock_config.evaluator_enabled = False
        self.cfg, self.db, self.dm = mock_config, db, dialogue_memory
        self.calls, self.tools_run, self.router_calls, self.planner_calls, self.extractor_calls = [], [], [], [], []
        self.diary_searches = []

    def run(self, responses, text="Make it quiet in here, I need to focus.", diary=()):
        from jarvis.reply import engine as engine_mod
        from jarvis.memory import conversation

        def fake_chat(cfg, messages, *, timeout_sec=30.0, extra_options=None, tools=None, thinking=False, model=None):
            self.calls.append({"model": model or cfg.llm_chat_model, "tools": tools,
                               "messages": [dict(m) for m in messages]})
            response = responses[min(len(self.calls) - 1, len(responses) - 1)]
            if isinstance(response, Exception):
                raise response
            return response

        def fake_tool(db, cfg, tool_name, tool_args, **kwargs):
            self.tools_run.append((tool_name, dict(tool_args or {})))
            return ToolExecutionResult(success=True, reply_text=f"{tool_name} done", error_message=None)

        def fake_diary(**kwargs):
            self.diary_searches.append(kwargs.get("keywords"))
            return list(diary)

        with patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool), \
             patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
             patch.object(engine_mod, "select_tools",
                          side_effect=lambda **k: self.router_calls.append(k) or ["systemVolume", "stop"]), \
             patch.object(engine_mod, "plan_query", side_effect=lambda **k: self.planner_calls.append(k) or []), \
             patch.object(engine_mod, "extract_search_params_for_memory",
                          side_effect=lambda *a, **k: self.extractor_calls.append(a) or {"keywords": []}), \
             patch.object(conversation, "search_conversation_memory_by_keywords", side_effect=fake_diary):
            return engine_mod.run_reply_engine(db=self.db, cfg=self.cfg, tts=None, text=text,
                                               dialogue_memory=self.dm)

    def system(self, index):
        return self.calls[index]["messages"][0]["content"]


@pytest.fixture
def harness(mock_config, db, dialogue_memory):
    return Harness(mock_config, db, dialogue_memory)


def _persona_marker():
    from jarvis.system_prompt import build_system_prompt
    return build_system_prompt("Jarvis").strip()[:120]


def test_the_tool_model_calls_the_tool_and_the_chat_model_writes_the_reply(harness):
    reply = harness.run([_call("systemVolume", {"action": "mute"}), _text("Muted."), _text("Silence, as requested.")])
    assert harness.tools_run == [("systemVolume", {"action": "mute"})]
    assert [c["model"] for c in harness.calls] == [TOOL_MODEL, TOOL_MODEL, CHAT_MODEL]
    assert harness.calls[0]["tools"] and harness.calls[1]["tools"] and harness.calls[2]["tools"] is None
    assert reply.strip() == "Silence, as requested."
    assert harness.router_calls == [] and harness.planner_calls == []


def test_the_tool_phase_uses_a_short_prompt_and_the_reply_phase_the_persona(harness):
    harness.run([_call("systemVolume", {"action": "mute"}), _text("Muted."), _text("Silence, as requested.")])
    assert _persona_marker() not in harness.system(0)
    assert _persona_marker() in harness.system(2)
    assert len(harness.system(0)) < len(harness.system(2)) / 2


def test_the_tool_model_sees_the_whole_catalogue_and_a_memory_tool(harness):
    from jarvis.tools.registry import BUILTIN_TOOLS
    harness.run([_text("Hello there."), _text("Good evening.")], text="Good evening, how are you?")
    names = {t["function"]["name"] for t in harness.calls[0]["tools"]}
    assert "recallMemory" in names and "stop" in names
    assert "toolSearchTool" not in names and "refreshMCPTools" not in names
    assert {n for n in BUILTIN_TOOLS if n not in ("toolSearchTool", "refreshMCPTools")} <= names


def test_a_conversational_request_goes_straight_to_the_reply_model(harness):
    reply = harness.run([_text("Doing well."), _text("Splendidly, thank you.")], text="How are you this evening?")
    assert harness.tools_run == [] and len(harness.calls) == 2 and reply.strip() == "Splendidly, thank you."
    note = harness.calls[1]["messages"][-1]
    assert note["role"] == "user" and "Doing well." in note["content"]


def test_memory_is_looked_up_only_when_the_tool_model_asks_with_its_own_keywords(harness):
    harness.run([_call("recallMemory", {"keywords": ["guitar"]}), _text("They play guitar."),
                 _text("You play the guitar, as I recall.")], text="What instrument do I play?",
                diary=["[2026-10-01] The user said they play the guitar."])
    assert harness.diary_searches == [["guitar"]] and harness.extractor_calls == []
    assert "play the guitar" in harness.system(2)
    assert harness.tools_run == []  # recallMemory is the engine's own lookup, not a registry tool
    tool_results = [m for m in harness.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_results and "play the guitar" in tool_results[-1]["content"]


def test_without_recall_no_memory_lookup_runs(harness):
    harness.run([_call("systemVolume", {"action": "mute"}), _text("Muted."), _text("Done.")])
    assert harness.diary_searches == [] and harness.extractor_calls == []


def test_a_failing_tool_model_leads_to_an_honest_tool_free_reply(harness):
    reply = harness.run([RuntimeError("model not found"), _text("I could not reach my tools just now.")])
    assert harness.tools_run == [] and harness.calls[-1]["model"] == CHAT_MODEL and harness.calls[-1]["tools"] is None
    assert "failed" in harness.calls[-1]["messages"][-1]["content"]
    assert reply.strip() == "I could not reach my tools just now."


def test_a_tool_call_in_the_reply_phase_is_not_run(harness):
    reply_turn = _call("systemVolume", {"action": "unmute"})
    reply_turn["message"]["content"] = "All quiet now."
    reply = harness.run([_call("systemVolume", {"action": "mute"}), _text("Muted."), reply_turn])
    assert harness.tools_run == [("systemVolume", {"action": "mute"})] and reply.strip() == "All quiet now."


def test_reasoning_traces_do_not_reach_the_reply_model(harness):
    harness.run([_call("systemVolume", {"action": "mute"}, thinking="secret plan"), _text("Muted."), _text("Done.")])
    assert all("thinking" not in m for m in harness.calls[2]["messages"])


def test_the_tool_phase_is_capped(harness):
    calls = [_call("getTime", {"location": f"City {n}"}) for n in range(10)]
    harness.run(calls[:6] + [_text("Here are the times.")])
    models = [c["model"] for c in harness.calls]
    assert models == [TOOL_MODEL] * 6 + [CHAT_MODEL]
    assert harness.calls[-1]["tools"] is None


def test_without_a_tool_model_the_normal_flow_runs(mock_config, db, dialogue_memory):
    harness = Harness(mock_config, db, dialogue_memory, tool_model="")
    harness.run([_call("systemVolume", {"action": "mute"}), _text("Muted.")])
    assert harness.router_calls and all(c["model"] == CHAT_MODEL for c in harness.calls)


def test_the_tool_tier_resolves_to_the_configured_tool_model(mock_config):
    from jarvis.llm import Tier, resolve_model
    mock_config.tool_model = " gpt-oss:20b "
    assert resolve_model(mock_config, Tier.TOOL) == "gpt-oss:20b"
    mock_config.tool_model = ""
    assert resolve_model(mock_config, Tier.TOOL) == ""


def test_the_setting_is_off_by_default_and_loads_from_config(tmp_path, monkeypatch):
    import json
    from jarvis import config
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tool_model": "gpt-oss:20b"}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
    assert config.get_default_config()["tool_model"] == ""
    assert config.load_settings().tool_model == "gpt-oss:20b"


def test_the_warm_profile_reaches_the_reply_phase_but_not_the_tool_phase(harness):
    from jarvis.reply import engine as engine_mod
    with patch("jarvis.memory.graph_ops.format_warm_profile_block", return_value="WARM PROFILE: the user likes jazz"):
        harness.run([_call("systemVolume", {"action": "mute"}), _text("Muted."), _text("Done.")])
    assert "likes jazz" not in harness.system(0) and "likes jazz" in harness.system(2)
