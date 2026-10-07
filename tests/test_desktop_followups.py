"""Follow-ups that refer to a window Jarvis just acted on ("open Word", then "move it ...").

Both turns run through ``run_reply_engine`` on a simulated desktop: the real fast path and window
tools execute, only the OS layer is fake. The model is a stand-in that records what it was given,
so these tests assert what reaches the model for the follow-up, in each reply mode.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from desktop_sim import SimulatedDesktop  # noqa: E402

from codex_bridge_fakes import FakeAppServer  # noqa: E402
from jarvis.bridge import modes  # noqa: E402
from jarvis.memory.desktop_referents import get_desktop_referents  # noqa: E402
from jarvis.tools.confirmation import get_confirmation_store  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop only")

FOLLOW_UP = "move it to the second monitor"


@pytest.fixture(autouse=True)
def isolated():
    get_confirmation_store().clear_pending()
    get_desktop_referents().clear()
    modes.reset()
    yield
    modes.reset()
    get_desktop_referents().clear()
    get_confirmation_store().clear_pending()


@pytest.fixture
def desktop(mock_config):
    with SimulatedDesktop(mock_config) as sim:
        yield sim


def _codex_service(cfg, script):
    from jarvis.codex_bridge.service import BridgeService
    server = FakeAppServer(script)
    service = BridgeService(cfg, server)
    modes.start(cfg, factories={"codex": lambda c: service}, save=lambda values: True)
    return service, server


def _fetching_model(seen):
    """A Codex stand-in that reads the request and asks which window was meant."""
    def script(model):
        seen.append(model.request)
        model.answer("needs_user_input", "Which window do you mean?")
        model.finish()
    return script


@pytest.mark.unit
class TestCodexFollowUp:
    @pytest.fixture
    def codex_cfg(self, mock_config):
        mock_config.reply_mode = "codex"
        mock_config.codex_enabled = True
        mock_config.codex_model = "gpt-6-luna"
        mock_config.codex_reasoning_effort = "low"
        mock_config.codex_timeout_sec = 10.0
        mock_config.codex_queue_limit = 1
        mock_config.codex_max_tool_calls = 8
        mock_config.codex_share_long_term_memory = False
        mock_config.codex_share_recent_dialogue = True
        return mock_config

    @pytest.mark.parametrize("dialogue_messages", [0, 6])
    def test_follow_up_request_identifies_the_window_jarvis_opened(
            self, codex_cfg, desktop, db, dialogue_memory, dialogue_messages):
        from jarvis.reply import engine

        codex_cfg.codex_recent_dialogue_messages = dialogue_messages
        seen = []
        service, _ = _codex_service(codex_cfg, _fetching_model(seen))
        try:
            engine.run_reply_engine(db, codex_cfg, None, "open Word", dialogue_memory, quiet=True)
            word = desktop.only("WINWORD")
            engine.run_reply_engine(db, codex_cfg, None, FOLLOW_UP, dialogue_memory, quiet=True)
        finally:
            service.close()
        assert len(seen) == 1, "the follow-up should reach Codex"
        # The tool catalogue lives in the jarvis_execute definition; the turn request holds only its own data.
        request = json.dumps(seen[0])
        assert str(word.hwnd) in request or "WINWORD" in request, (
            f"Codex cannot tell which window 'it' is: {request}")

    def test_acting_on_the_first_record_moves_the_window_jarvis_opened(self, codex_cfg, desktop, db,
                                                                       dialogue_memory):
        from desktop_sim import PRIMARY, SECOND
        from jarvis.reply import engine

        codex_cfg.codex_recent_dialogue_messages = 0
        older = desktop.spawn("WINWORD", PRIMARY)

        def script(model):
            payload = model.request
            target = str(payload["desktop_referents"][0]["hwnd"])
            model.execute("windowControl", {"action": "place", "target": target, "monitor": "2"})
            model.answer("completed", "Moved it.")
            model.finish()

        service, _ = _codex_service(codex_cfg, script)
        try:
            engine.run_reply_engine(db, codex_cfg, None, "open Word", dialogue_memory, quiet=True)
            reply = engine.run_reply_engine(db, codex_cfg, None, FOLLOW_UP, dialogue_memory, quiet=True)
        finally:
            service.close()
        new = next(w for w in desktop.open_windows("WINWORD") if w.hwnd != older.hwnd)
        assert reply == "Moved it."
        assert (new.monitor, older.monitor) == (SECOND, PRIMARY)

    def test_a_fast_window_command_by_voice_is_remembered_for_the_next_request(self, codex_cfg, desktop, db,
                                                                               dialogue_memory):
        from desktop_sim import PRIMARY
        from jarvis.reply import engine

        codex_cfg.codex_recent_dialogue_messages = 0
        notepad = desktop.spawn("notepad", PRIMARY)
        seen = []
        service, server = _codex_service(codex_cfg, _fetching_model(seen))
        try:
            fast = engine.run_reply_engine(db, codex_cfg, None, "maximise Notepad", dialogue_memory,
                                           language="en")
            assert server.turns == [], "a deterministic command runs locally with no Codex session"
            engine.run_reply_engine(db, codex_cfg, None, "move it to monitor 2", dialogue_memory, language="en")
        finally:
            service.close()
        assert fast and notepad.state == "maximised"
        (record,) = [r for r in seen[0].get("desktop_referents", []) if r["hwnd"] == notepad.hwnd]
        assert (record["state"], record["last_action"]) == ("maximised", "maximise")

    def test_with_sharing_off_codex_gets_no_window_record(self, codex_cfg, desktop, db, dialogue_memory):
        from jarvis.reply import engine

        codex_cfg.codex_recent_dialogue_messages = 0
        codex_cfg.codex_share_desktop_referents = False
        codex_cfg.codex_share_foreground_window = False  # the launched window is also in front
        seen = []
        service, _ = _codex_service(codex_cfg, _fetching_model(seen))
        try:
            engine.run_reply_engine(db, codex_cfg, None, "open Word", dialogue_memory, quiet=True)
            word = desktop.only("WINWORD")
            engine.run_reply_engine(db, codex_cfg, None, FOLLOW_UP, dialogue_memory, quiet=True)
        finally:
            service.close()
        request = json.dumps(seen[0])
        assert "desktop_referents" not in seen[0] and str(word.hwnd) not in request


@pytest.mark.unit
class TestLocalFollowUp:
    def test_follow_up_model_context_identifies_the_window_jarvis_opened(
            self, mock_config, desktop, db, dialogue_memory, monkeypatch):
        from jarvis.reply import engine

        mock_config.reply_mode = "local"
        seen = {}

        def chat(cfg, messages, **kwargs):
            seen.setdefault("messages", [dict(m) for m in messages])
            return {"message": {"content": "Which window do you mean?"}}

        def plan(cfg, query, dialogue_context, tools, **kwargs):
            seen.setdefault("planner_context", dialogue_context)
            return []

        monkeypatch.setattr(engine, "chat_with_messages", chat)
        monkeypatch.setattr(engine, "plan_query", plan)
        def route(**kw):
            seen.setdefault("router_hint", kw.get("context_hint") or "")
            return ["windowControl", "appControl"]

        monkeypatch.setattr(engine, "select_tools", route)
        monkeypatch.setattr(engine, "extract_search_params_for_memory", lambda *a, **kw: {})

        engine.run_reply_engine(db, mock_config, None, "open Word", dialogue_memory, quiet=True)
        word = desktop.only("WINWORD")
        engine.run_reply_engine(db, mock_config, None, FOLLOW_UP, dialogue_memory, quiet=True)

        # The previous fast turn stays in the conversation the small model sees.
        dialogue = [(m["role"], m["content"]) for m in seen["messages"][1:]]
        assert dialogue[:2] == [("user", "open Word"), ("assistant", "Opening Word.")]
        assert dialogue[-1] == ("user", FOLLOW_UP)
        model_view = json.dumps(seen["messages"])
        assert str(word.hwnd) in model_view or "WINWORD" in model_view, (
            "the chat model cannot tell which window 'it' is")
        assert str(word.hwnd) in seen["planner_context"] or "WINWORD" in seen["planner_context"], (
            "the planner cannot tell which window 'it' is")
        hint = seen["router_hint"]
        dialogue = hint.partition("Recent dialogue (short-term memory):")[2]
        assert str(word.hwnd) in dialogue or "WINWORD" in dialogue, (
            "the router should read the window record as part of the conversation, not as known facts")

    def test_window_tools_stay_available_while_jarvis_has_just_acted_on_a_window(
            self, mock_config, desktop, db, dialogue_memory, monkeypatch):
        """A small router can miss that "move it" is about a window; the engine keeps the tools."""
        from jarvis.reply import engine

        offered = []

        def chat(cfg, messages, tools=None, **kwargs):
            offered.append({t["function"]["name"] for t in (tools or [])})
            return {"message": {"content": "Which window?"}}

        monkeypatch.setattr(engine, "chat_with_messages", chat)
        monkeypatch.setattr(engine, "plan_query", lambda *a, **kw: [])
        monkeypatch.setattr(engine, "select_tools", lambda **kw: ["webSearch"])  # the router misses
        monkeypatch.setattr(engine, "extract_search_params_for_memory", lambda *a, **kw: {})
        mock_config.ollama_chat_model = mock_config.llm_chat_model = "gpt-oss:20b"  # native tool schema

        engine.run_reply_engine(db, mock_config, None, "hello", dialogue_memory, quiet=True)
        assert not offered[-1] & {"windowControl", "appControl"}, "no window was acted on yet"
        engine.run_reply_engine(db, mock_config, None, "open Word", dialogue_memory, quiet=True)
        engine.run_reply_engine(db, mock_config, None, FOLLOW_UP, dialogue_memory, quiet=True)
        assert {"windowControl", "appControl"} <= offered[-1]

    def test_no_window_record_is_added_when_jarvis_has_not_acted_on_one(
            self, mock_config, desktop, db, dialogue_memory, monkeypatch):
        from jarvis.reply import engine

        seen = {}

        def chat(cfg, messages, **kwargs):
            seen.setdefault("system", messages[0]["content"])
            return {"message": {"content": "Hello."}}

        monkeypatch.setattr(engine, "chat_with_messages", chat)
        monkeypatch.setattr(engine, "plan_query", lambda *a, **kw: [])
        monkeypatch.setattr(engine, "select_tools", lambda **kw: ["windowControl"])
        monkeypatch.setattr(engine, "extract_search_params_for_memory", lambda *a, **kw: {})
        engine.run_reply_engine(db, mock_config, None, "hello there", dialogue_memory, quiet=True)
        assert "recently opened, focused or placed" not in seen["system"]

    def test_the_window_the_user_is_looking_at_reaches_the_model_without_widening_tools(
            self, mock_config, desktop, db, dialogue_memory, monkeypatch):
        """'close this' with nothing done yet: the model sees the foreground window by handle, and the
        router alone decides the tools."""
        from jarvis.reply import engine

        chrome = desktop.only("chrome")
        desktop.foreground = chrome.hwnd
        seen = {"offered": []}

        def chat(cfg, messages, tools=None, **kwargs):
            seen.setdefault("system", messages[0]["content"])
            seen["offered"].append({t["function"]["name"] for t in (tools or [])})
            return {"message": {"content": "Which window?"}}

        monkeypatch.setattr(engine, "chat_with_messages", chat)
        monkeypatch.setattr(engine, "plan_query", lambda *a, **kw: [])
        monkeypatch.setattr(engine, "select_tools", lambda **kw: ["webSearch"])
        monkeypatch.setattr(engine, "extract_search_params_for_memory", lambda *a, **kw: {})
        mock_config.ollama_chat_model = mock_config.llm_chat_model = "gpt-oss:20b"  # native tool schema
        engine.run_reply_engine(db, mock_config, None, "close this", dialogue_memory, quiet=True)
        assert str(chrome.hwnd) in seen["system"] and "Google Chrome" in seen["system"]
        assert "New Tab" not in seen["system"], "a window title reached the model"
        assert not seen["offered"][-1] & {"windowControl", "appControl"}

    def test_a_phone_request_never_sees_the_window_in_front_of_the_pc(
            self, mock_config, desktop, db, dialogue_memory, monkeypatch):
        from jarvis.reply import engine

        chrome = desktop.only("chrome")
        desktop.foreground = chrome.hwnd
        seen = {}

        def chat(cfg, messages, **kwargs):
            seen.setdefault("system", messages[0]["content"])
            return {"message": {"content": "Which window?"}}

        def route(**kw):
            seen.setdefault("router_hint", kw.get("context_hint") or "")
            return ["webSearch"]

        monkeypatch.setattr(engine, "chat_with_messages", chat)
        monkeypatch.setattr(engine, "plan_query", lambda *a, **kw: [])
        monkeypatch.setattr(engine, "select_tools", route)
        monkeypatch.setattr(engine, "extract_search_params_for_memory", lambda *a, **kw: {})
        engine.run_reply_engine(db, mock_config, None, "close this", dialogue_memory, quiet=True, origin="phone")
        assert str(chrome.hwnd) not in seen["system"] and "chrome" not in seen["router_hint"].lower()

    def test_the_tool_of_a_live_media_record_stays_available(
            self, mock_config, desktop, db, dialogue_memory, monkeypatch):
        """'pause it' after Jarvis played music keeps mediaControl even when the router misses."""
        from jarvis.reply import engine
        from jarvis.tools.registry import BUILTIN_TOOLS

        if "mediaControl" not in BUILTIN_TOOLS:
            pytest.skip("mediaControl is registered on Windows only")
        offered = []

        def chat(cfg, messages, tools=None, **kwargs):
            offered.append({t["function"]["name"] for t in (tools or [])})
            return {"message": {"content": "Which one?"}}

        monkeypatch.setattr(engine, "chat_with_messages", chat)
        monkeypatch.setattr(engine, "plan_query", lambda *a, **kw: [])
        monkeypatch.setattr(engine, "select_tools", lambda **kw: ["webSearch"])
        monkeypatch.setattr(engine, "extract_search_params_for_memory", lambda *a, **kw: {})
        mock_config.ollama_chat_model = mock_config.llm_chat_model = "gpt-oss:20b"
        get_desktop_referents().record_media("Apple Music", "playing")
        engine.run_reply_engine(db, mock_config, None, "pause it", dialogue_memory, quiet=True)
        assert "mediaControl" in offered[-1]
        assert not offered[-1] & {"windowControl", "appControl"}, "no window was acted on"
