"""gestureControl: start, stop and status through the normal tool path, registration and cloud exclusion."""
import pytest

from tests.synthetic_hands import FakeDesktop
from tests.test_window_snap_service import FakeTracker


@pytest.fixture
def fake_service(mock_config, monkeypatch):
    from jarvis.gestures import window_snap_service
    desk = FakeDesktop()
    FakeTracker.instances = []
    svc = window_snap_service.WindowSnapService(mock_config, output_factory=lambda: desk,
                                            tracker_factory=lambda camera: FakeTracker(camera))
    monkeypatch.setattr(window_snap_service, "_service", svc)
    yield svc
    svc.stop()


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    from jarvis.gestures import window_snap_service
    from jarvis.tools.registry import BUILTIN_TOOLS
    monkeypatch.setattr(window_snap_service, "_service", None)
    BUILTIN_TOOLS.pop("gestureControl", None)
    yield
    BUILTIN_TOOLS.pop("gestureControl", None)


def run(cfg, **args):
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
    BUILTIN_TOOLS.setdefault("gestureControl", GestureControlTool())
    return run_tool_with_retries(db=None, cfg=cfg, tool_name="gestureControl", tool_args=args, system_prompt="",
                                 original_prompt="", redacted_text="", max_retries=1, quiet=True)


def test_start_turns_the_gesture_snap_on(fake_service, mock_config):
    result = run(mock_config, action="start")
    assert result.success and fake_service.running
    from jarvis.gestures.window_snap_service import GESTURE_GUIDE
    assert GESTURE_GUIDE in result.reply_text  # the reply tells the user how to use it


def test_start_when_on_says_so_and_keeps_one_camera(fake_service, mock_config):
    run(mock_config, action="start")
    result = run(mock_config, action="start")
    assert result.success and "already" in result.reply_text.lower()
    assert len(FakeTracker.instances) == 1


def test_stop_turns_it_off_and_releases_the_camera(fake_service, mock_config):
    run(mock_config, action="start")
    result = run(mock_config, action="stop")
    assert result.success and not fake_service.running
    assert not FakeTracker.instances[-1].running


def test_stop_when_off_succeeds(fake_service, mock_config):
    assert run(mock_config, action="stop").success


def test_status_reports_whether_it_is_on(fake_service, mock_config):
    off = run(mock_config, action="status")
    run(mock_config, action="start")
    on = run(mock_config, action="status")
    assert off.success and on.success and off.reply_text != on.reply_text


def test_a_start_failure_is_reported_honestly(mock_config, monkeypatch):
    from jarvis.gestures import window_snap_service
    svc = window_snap_service.WindowSnapService(mock_config, output_factory=FakeDesktop,
                                            tracker_factory=lambda camera: FakeTracker(camera, works=False))
    monkeypatch.setattr(window_snap_service, "_service", svc)
    result = run(mock_config, action="start")
    assert not result.success and result.error_message


def test_an_unknown_action_fails(fake_service, mock_config):
    assert not run(mock_config, action="explode").success


def test_every_action_is_routine(mock_config):
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    for action in ("start", "stop", "status"):
        safety = evaluate_safety("gestureControl", {"action": action}, mock_config, tool=GestureControlTool())
        assert safety.tier == SafetyTier.SAFE


@pytest.mark.parametrize("enabled,platform,offered", [(True, "win32", True), (False, "win32", False),
                                                       (True, "linux", False)])
def test_offered_only_when_enabled_in_settings_on_windows(mock_config, enabled, platform, offered):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_gesture_tool
    mock_config.gesture_snap_enabled = enabled
    configure_gesture_tool(mock_config, platform=platform)
    assert ("gestureControl" in BUILTIN_TOOLS) is offered


def test_turning_the_setting_off_stops_running_gesture_snapping(fake_service, mock_config):
    from jarvis.tools.registry import configure_gesture_tool
    fake_service.start()
    mock_config.gesture_snap_enabled = False
    configure_gesture_tool(mock_config, platform="win32")
    assert not fake_service.running


def test_never_offered_to_a_cloud_model(mock_config):
    from jarvis.bridge.tools import build_tool_snapshot
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    BUILTIN_TOOLS["gestureControl"] = GestureControlTool()
    for share in (False, True):
        assert "gestureControl" not in build_tool_snapshot(mock_config, share_long_term_memory=share,
                                                           share_lists_reminders=share)


def _desk(svc):
    return svc._output_factory()


@pytest.mark.parametrize("action", ["click", "double_click", "right_click"])
def test_a_spoken_click_clicks_at_the_pointer_while_gesture_snapping_is_on(fake_service, mock_config, action):
    run(mock_config, action="start")
    result = run(mock_config, action=action)
    assert result.success
    assert [e[1] for e in _desk(fake_service).of("click")] == [action]


def test_a_click_while_gesture_snapping_is_off_is_refused(fake_service, mock_config):
    result = run(mock_config, action="click")
    assert not result.success and "gesture control is on" in result.reply_text
    assert _desk(fake_service).of("click") == []


def test_a_click_while_holding_a_window_is_refused(fake_service, mock_config):
    from tests.synthetic_hands import FakeWindow
    from tests.test_window_snap_service import grab_and_hold
    _desk(fake_service).windows.append(FakeWindow((0, 0, 1920, 1080)))
    run(mock_config, action="start")
    grab_and_hold(FakeTracker.instances[-1])
    result = run(mock_config, action="click")
    assert not result.success and "window" in result.reply_text.lower()


def test_a_click_windows_blocks_is_reported(fake_service, mock_config):
    run(mock_config, action="start")
    _desk(fake_service).fail = True
    result = run(mock_config, action="click")
    assert not result.success and "blocked" in result.reply_text.lower()


def test_a_phone_cannot_click(fake_service, mock_config):
    from jarvis.tools.request_scope import request_scope
    run(mock_config, action="start")
    with request_scope("phone"):
        result = run(mock_config, action="click")
    assert not result.success and "at the PC" in result.reply_text
    assert _desk(fake_service).of("click") == []


def test_click_actions_are_routine(mock_config):
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.confirmation import SafetyTier, evaluate_safety
    for action in ("click", "double_click", "right_click"):
        safety = evaluate_safety("gestureControl", {"action": action}, mock_config, tool=GestureControlTool())
        assert safety.tier == SafetyTier.SAFE


def test_click_routes_are_fast_only_while_gesture_snapping_runs(fake_service, mock_config):
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    tool = GestureControlTool()
    assert tool.fast_available(mock_config, {"action": "start"})
    assert not tool.fast_available(mock_config, {"action": "click"})
    fake_service.start()
    assert tool.fast_available(mock_config, {"action": "click"})
