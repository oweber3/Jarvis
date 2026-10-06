"""
Activity Log Evaluations (Live)

"What was I working on yesterday afternoon?" against a seeded activity database. The real
``activityLog`` tool runs over a scratch database holding sessions for yesterday morning, yesterday
afternoon and today; the model has to choose the tool, turn "yesterday afternoon" into an ISO range
using the date it is given, and answer from the returned aggregates alone. The reply must also stay
out of the diary.

Run: ./scripts/run_evals.sh test_activity_log
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from conftest import requires_judge_llm
from helpers import JUDGE_MODEL, ToolCallCapture


def _at(day_offset: int, hour: int, minute: int = 0) -> float:
    day = datetime.now() + timedelta(days=day_offset)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def _seed(db_path: str) -> None:
    from jarvis.memory.activity_log import ActivityStore

    store = ActivityStore(db_path)
    sessions = [
        # Yesterday morning: gaming and chat.
        (-1, 9, 0, 11, 30, "Minecraft", "Minecraft", "Minecraft"),
        (-1, 11, 30, 11, 55, "Discord", "Discord", "#general - Discord"),
        # Yesterday afternoon: a budget model.
        (-1, 13, 0, 15, 0, "MATLAB", "MATLAB R2025b", "budget_model.m - MATLAB"),
        (-1, 15, 0, 15, 40, "chrome", "Google Chrome", "ode45 - MATLAB Documentation"),
        (-1, 15, 40, 17, 0, "WINWORD", "Microsoft Word", "Budget report draft - Word"),
        # Today: something else entirely.
        (0, 7, 0, 7, 20, "Code", "Visual Studio Code", "README.md - jarvis"),
    ]
    for day, h1, m1, h2, m2, process, app, title in sessions:
        store.open_session(_at(day, h1, m1), process, app, title, end=_at(day, h2, m2))
    store.close()


@pytest.fixture
def activity_cfg(mock_config, tmp_path):
    from jarvis.tools import registry

    mock_config.db_path = str(tmp_path / "activity.db")
    mock_config.activity_log_enabled = True
    mock_config.activity_log_share_with_cloud = False
    mock_config.location_enabled = False
    _seed(mock_config.db_path)
    original = dict(registry.BUILTIN_TOOLS)
    registry.configure_activity_log_tool(mock_config, platform="win32")
    try:
        yield mock_config
    finally:
        registry.BUILTIN_TOOLS.clear()
        registry.BUILTIN_TOOLS.update(original)


def _run(query, cfg, db, memory):
    """Run the real engine with the real tool, recording each tool call."""
    from jarvis.reply.engine import run_reply_engine
    from jarvis.tools.registry import run_tool_with_retries

    capture = ToolCallCapture()

    def recording(db_, cfg_, tool_name, tool_args, *args, **kwargs):
        capture.record(tool_name, tool_args or {})
        return run_tool_with_retries(db_, cfg_, tool_name, tool_args, *args, **kwargs)

    cfg.ollama_chat_model = JUDGE_MODEL
    with patch("jarvis.reply.engine.run_tool_with_retries", side_effect=recording):
        reply = run_reply_engine(db=db, cfg=cfg, tts=None, text=query, dialogue_memory=memory, quiet=True)
    return capture, reply or ""


def _range_of(call):
    from jarvis.memory.activity_log import parse_time

    args = call["args"]
    start = parse_time(args["start"])
    end = parse_time(args["end"]) if args.get("end") else datetime.now().timestamp()
    return start, end


class TestActivityLogLive:
    @pytest.mark.eval
    @requires_judge_llm
    @pytest.mark.parametrize("query,block,expected,forbidden", [
        pytest.param("What was I working on yesterday afternoon?", (-1, 13, 17),
                     ["matlab", "budget"], ["minecraft", "discord"], id="yesterday afternoon"),
        pytest.param("How did I spend yesterday morning?", (-1, 9, 12),
                     ["minecraft"], ["matlab", "budget"], id="yesterday morning"),
    ])
    def test_answers_from_the_activity_log(self, query, block, expected, forbidden, activity_cfg, eval_db,
                                           eval_dialogue_memory):
        capture, reply = _run(query, activity_cfg, eval_db, eval_dialogue_memory)

        print(f"\n  Activity eval ({JUDGE_MODEL}): {query!r}")
        print(f"  Tools: {capture.tool_names() or 'none'}")
        print(f"  Args: {json.dumps(capture.get_args('activityLog'))}")
        print(f"  Reply: {reply[:240]}")

        assert capture.has_tool("activityLog"), f"activityLog was not used; tools: {capture.tool_names()}"
        call = next(c for c in capture.calls if c["name"] == "activityLog")
        assert call["args"].get("action") in ("summary", "timeline")
        start, end = _range_of(call)
        day, first, last = block
        assert start < _at(day, last) and end > _at(day, first), "the range misses the asked-about period"
        assert start >= _at(day, 0) - 3600 and end <= _at(day + 1, 0) + 3600, "the range wanders off the day"

        lowered = reply.lower()
        assert any(word in lowered for word in expected), f"reply lacks {expected}: {reply!r}"
        assert not any(word in lowered for word in forbidden), f"reply mixes in another period: {reply!r}"

        # The answer quotes activity data, so the turn must not reach the diary.
        assert eval_dialogue_memory.get_pending_chunks() == []

    @pytest.mark.eval
    @requires_judge_llm
    def test_unrelated_requests_do_not_touch_the_log(self, activity_cfg, eval_db, eval_dialogue_memory):
        capture, reply = _run("Tell me a short joke about penguins.", activity_cfg, eval_db,
                              eval_dialogue_memory)
        print(f"\n  Tools: {capture.tool_names() or 'none'}\n  Reply: {reply[:120]}")
        assert not capture.has_tool("activityLog")
        assert eval_dialogue_memory.get_pending_chunks() != []  # an ordinary turn is diarised as usual
