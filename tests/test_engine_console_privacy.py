"""The agent loop's console lines never show the words of a tool whose arguments are the user's own
(``log_arguments`` false: device names, search phrases, mail senders, reminder text), nor its error text,
which can quote the same (smarthome.spec.md, documents.spec.md, mail.spec.md)."""
import pytest

from jarvis.reply import engine
from jarvis.tools.registry import BUILTIN_TOOLS


def _tool_class(tool):
    from jarvis.tools.builtin.activity_log import ActivityLogTool
    from jarvis.tools.builtin.calendar_tool import CalendarTool
    from jarvis.tools.builtin.document_search import DocumentSearchTool
    from jarvis.tools.builtin.email_tool import EmailTool
    from jarvis.tools.builtin.home_control import HomeControlTool
    from jarvis.tools.builtin.routine_control import RoutineControlTool
    from jarvis.tools.builtin.schedule_control import ScheduleControlTool
    return {"homeControl": HomeControlTool, "documentSearch": DocumentSearchTool, "email": EmailTool,
            "calendar": CalendarTool, "activityLog": ActivityLogTool, "routineControl": RoutineControlTool,
            "scheduleControl": ScheduleControlTool}[tool]


@pytest.mark.unit
@pytest.mark.parametrize("tool", ["homeControl", "documentSearch", "email", "calendar", "activityLog",
                                  "routineControl", "scheduleControl"])
def test_a_tool_with_private_arguments_shows_only_their_names(tool, monkeypatch):
    cls = _tool_class(tool)
    monkeypatch.setitem(BUILTIN_TOOLS, tool, cls.__new__(cls))   # registered as when its feature is on
    line = engine._console_tool_call(tool, {"query": "oncologist referral", "device": "Front Door"})
    assert tool in line and "query" in line
    assert "oncologist" not in line and "Front Door" not in line
    error = engine._console_tool_error(tool, "Front Door did not answer")
    assert "Front Door" not in error


@pytest.mark.unit
def test_other_tools_still_show_redacted_arguments_and_errors():
    line = engine._console_tool_call("webSearch", {"query": "weather in Leeds", "email": "me@example.com"})
    assert "weather in Leeds" in line and "me@example.com" not in line
    assert "timed out" in engine._console_tool_error("webSearch", "The search timed out")
