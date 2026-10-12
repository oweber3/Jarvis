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
    assert "pinch" in result.reply_text.lower()


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
        assert "gestureControl" not in build_tool_snapshot(mock_config, share_long_term_memory=share)
