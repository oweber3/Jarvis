"""Windows that run as administrator are refused plainly instead of reading as empty."""
import pytest

from jarvis.platform.windows import ui_automation as ua


class _NoWalk:
    """A UIA client whose tree must never be read."""

    class uia:
        @staticmethod
        def ElementFromHandle(hwnd):
            pytest.fail('an administrator window was walked')


@pytest.fixture
def admin_window(monkeypatch):
    monkeypatch.setattr(ua, 'resolve_hwnd', lambda window='': 77)
    monkeypatch.setattr(ua, 'window_blocked', lambda hwnd: hwnd == 77)
    monkeypatch.setattr(ua, '_with_uia', lambda fn, *args: fn(_NoWalk(), *args))


def test_a_snapshot_of_an_administrator_window_says_so(admin_window):
    with pytest.raises(ValueError, match='administrator'):
        ua.snapshot('Task Manager')


def test_an_action_on_an_administrator_window_says_so(admin_window):
    with pytest.raises(ValueError, match='administrator'):
        ua.act('click', 'Task Manager', 'End task')


def test_the_tool_reports_the_refusal_as_a_failure(admin_window):
    from jarvis.config import load_settings
    from jarvis.tools.base import ToolContext
    from jarvis.tools.builtin.windows import UiControlTool
    result = UiControlTool().run({'action': 'snapshot', 'window': 'Task Manager'},
                                 ToolContext(None, load_settings(), '', '', '', 1, lambda text: None))
    assert not result.success and 'administrator' in result.error_message
