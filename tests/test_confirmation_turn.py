"""A tool call that needs confirmation ends the reply with Jarvis's own question.

Only one confirmation can be pending. If the reply loop kept going after the first held action,
a second held action would silently replace it, and the model's paraphrase could ask about the
first while a "yes" ran the second (reply.spec.md, Action Safety). These tests drive the real
engine loop and the real safety gate with a scripted model.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from jarvis.reply import engine as engine_mod
from jarvis.tools.base import Tool
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    SafetyTier,
    get_confirmation_store,
    set_dialog_callback,
    set_result_handler,
)
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult


class HeldTool(Tool):
    """A destructive tool that needs a spoken yes; records every run."""

    def __init__(self, name: str, tier: SafetyTier = SafetyTier.CONFIRM_VOICE):
        self._name, self._tier = name, tier
        self.runs: list[str] = []

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "Delete a file."

    @property
    def inputSchema(self):
        return {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

    def classify_safety(self, args, cfg):
        return ConfirmationRequest(
            tool_name=self._name, tier=self._tier, action="delete file",
            target=str((args or {}).get("target", "")), parameters=dict(args or {}),
        )

    def run(self, args, context):
        self.runs.append(str((args or {}).get("target", "")))
        return ToolExecutionResult(success=True, reply_text="Deleted.")


class FakeDialogs:
    def __init__(self):
        self.shown = []

    def __call__(self, request, resolve):
        self.shown.append((request, resolve))
        return self

    def close(self):
        pass


@pytest.fixture(autouse=True)
def clean_state():
    store = get_confirmation_store()
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)
    yield
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)


@pytest.fixture
def held_tool():
    tool = HeldTool("heldDelete")
    BUILTIN_TOOLS[tool.name] = tool
    yield tool
    BUILTIN_TOOLS.pop(tool.name, None)


@pytest.fixture
def cfg(mock_config):
    mock_config.fast_commands_enabled = False
    mock_config.evaluator_enabled = False
    return mock_config


def _tool_call(name, target):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{
        "id": f"call_{target}", "type": "function",
        "function": {"name": name, "arguments": {"target": target}},
    }]}}


def _text(content):
    return {"message": {"role": "assistant", "content": content}}


class GateSpy:
    """The real safety gate, remembering what each call returned."""

    def __init__(self):
        self.results: list[ToolExecutionResult] = []

    def __call__(self, *args, **kwargs):
        result = run_tool_with_retries(*args, **kwargs)
        self.results.append(result)
        return result


def _run(cfg, db, dialogue_memory, text, *, chat, plan=(), resolve=None, tools=("heldDelete", "stop")):
    gate = GateSpy()
    patches = [
        patch.object(engine_mod, "run_tool_with_retries", side_effect=gate),
        patch.object(engine_mod, "chat_with_messages", side_effect=chat),
        patch.object(engine_mod, "select_tools", return_value=list(tools)),
        patch.object(engine_mod, "extract_search_params_for_memory", return_value={"keywords": []}),
        patch.object(engine_mod, "plan_query", return_value=list(plan)),
    ]
    if resolve is not None:
        patches.append(patch.object(engine_mod, "_resolve_plan_step", side_effect=resolve))
    for p in patches:
        p.start()
    try:
        reply = engine_mod.run_reply_engine(db=db, cfg=cfg, tts=None, text=text,
                                            dialogue_memory=dialogue_memory, quiet=True)
    finally:
        for p in reversed(patches):
            p.stop()
    return reply, gate


def _answer_yes(cfg, db, dialogue_memory):
    return engine_mod.run_reply_engine(db=db, cfg=cfg, tts=None, text="yes",
                                       dialogue_memory=dialogue_memory, quiet=True)


def test_first_held_action_ends_the_reply_with_its_own_question(held_tool, cfg, db, dialogue_memory):
    cfg.ollama_chat_model = cfg.llm_chat_model = "gpt-oss:20b"  # native tool calling
    model_turns = [
        _tool_call("heldDelete", "a.txt"),
        _tool_call("heldDelete", "b.txt"),
        _text("Shall I delete a.txt?"),
    ]
    reply, gate = _run(cfg, db, dialogue_memory, "delete a.txt and b.txt", chat=model_turns)

    # The question is the gate's own wording for the held action, not the model's paraphrase.
    assert len(gate.results) == 1
    assert reply == gate.results[0].reply_text
    pending = get_confirmation_store().get_pending()
    assert pending is not None and pending.request.target == "a.txt"
    # The held call is answered by its question in the context the next reply sees.
    carried = dialogue_memory.get_recent_turns_with_tools(max_tool_turns=2, per_entry_chars=1200)
    assert any(m.get("role") == "tool" and m.get("content") == reply for m in carried)

    _answer_yes(cfg, db, dialogue_memory)
    assert held_tool.runs == ["a.txt"]


def test_small_model_plan_stops_at_the_first_held_step(held_tool, cfg, db, dialogue_memory):
    cfg.ollama_chat_model = cfg.llm_chat_model = "gemma4:e2b"  # text tools, plan direct-exec
    steps = iter([("heldDelete", {"target": "a.txt"}), ("heldDelete", {"target": "b.txt"})])
    plan = ["heldDelete target='a.txt'", "heldDelete target='b.txt'", "Reply to the user."]

    reply, gate = _run(
        cfg, db, dialogue_memory, "delete a.txt and b.txt",
        chat=lambda *a, **k: _text("Shall I delete a.txt?"), plan=plan,
        resolve=lambda *a, **k: next(steps, None),
    )

    assert len(gate.results) == 1
    assert reply == gate.results[0].reply_text
    _answer_yes(cfg, db, dialogue_memory)
    assert held_tool.runs == ["a.txt"]


def test_desktop_confirmation_ends_the_reply_and_keeps_its_dialog(cfg, db, dialogue_memory):
    tool = HeldTool("heldUninstall", SafetyTier.CONFIRM_DIALOG)
    BUILTIN_TOOLS[tool.name] = tool
    dialogs = FakeDialogs()
    set_dialog_callback(dialogs)
    cfg.ollama_chat_model = cfg.llm_chat_model = "gpt-oss:20b"
    try:
        reply, gate = _run(
            cfg, db, dialogue_memory, "remove a and b",
            chat=[_tool_call(tool.name, "a"), _tool_call(tool.name, "b"), _text("Done.")],
            tools=(tool.name, "stop"),
        )
    finally:
        BUILTIN_TOOLS.pop(tool.name, None)

    assert len(dialogs.shown) == 1
    assert reply == gate.results[0].reply_text
    pending = get_confirmation_store().get_pending()
    assert pending is not None and pending.request.target == "a"
    assert tool.runs == []


def test_routine_tools_before_the_held_action_still_run(held_tool, cfg, db, dialogue_memory):
    class Safe(HeldTool):
        def classify_safety(self, args, cfg):
            return ConfirmationRequest(self._name, SafetyTier.SAFE, "open", "", dict(args or {}))

    safe = Safe("safeOpen")
    BUILTIN_TOOLS[safe.name] = safe
    cfg.ollama_chat_model = cfg.llm_chat_model = "gpt-oss:20b"
    try:
        reply, gate = _run(
            cfg, db, dialogue_memory, "open notes then delete a.txt",
            chat=[_tool_call(safe.name, "notes"), _tool_call("heldDelete", "a.txt"), _text("All done.")],
            tools=(safe.name, "heldDelete", "stop"),
        )
    finally:
        BUILTIN_TOOLS.pop(safe.name, None)

    assert safe.runs == ["notes"]
    assert reply == gate.results[-1].reply_text
    assert held_tool.runs == []


def test_tool_model_mode_ends_the_reply_before_the_reply_phase(held_tool, cfg, db, dialogue_memory):
    cfg.ollama_chat_model = cfg.llm_chat_model = "qwen3.5:9b"
    cfg.tool_model = "gpt-oss:20b"
    models = []

    def chat(cfg, messages, *, model=None, **kwargs):
        models.append(model)
        turns = [_tool_call("heldDelete", "a.txt"), _tool_call("heldDelete", "b.txt"), _text("Deleted both.")]
        return turns[min(len(models), len(turns)) - 1]

    reply, gate = _run(cfg, db, dialogue_memory, "delete a.txt and b.txt", chat=chat)

    assert models == ["gpt-oss:20b"]  # the chat model never paraphrases the question
    assert len(gate.results) == 1
    assert reply == gate.results[0].reply_text
    _answer_yes(cfg, db, dialogue_memory)
    assert held_tool.runs == ["a.txt"]
