"""Gesture snapping keeps everything the camera sees on this machine.

No frame, landmark, gesture reading, pointer position or window it touched reaches a cloud model, a log, a tool
result or disk, and nothing a cloud model does can turn the camera on. See ``gestures/gestures.spec.md``,
Privacy and logs.
"""
import os
import re
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.gestures import hand_tracking, snap_state, window_snap, window_snap_service
from jarvis.gestures.hand_tracking import HandTracker
from jarvis.gestures.window_snap_service import WindowSnapService
from tests.synthetic_hands import FakeDesktop, FakeWindow, frame, hand

TITLE = "Payroll 2026 - Confidential.xlsx"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    from jarvis.tools.registry import BUILTIN_TOOLS
    monkeypatch.setattr(window_snap_service, "_service", None)
    BUILTIN_TOOLS.pop("gestureControl", None)
    yield
    BUILTIN_TOOLS.pop("gestureControl", None)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """The shared on flag in a file of the test's own."""
    path = tmp_path / "jarvis_gesture_snap"
    monkeypatch.setattr(snap_state, "_channel", snap_state.SnapStateChannel(str(path)))
    return path


@pytest.fixture
def logs(monkeypatch):
    """Everything gesture snapping debug-logs, with debug logging on."""
    lines = []
    for module in (hand_tracking, window_snap, window_snap_service, snap_state):
        monkeypatch.setattr(module, "debug_log", lambda message, category="debug": lines.append(message))
    return lines


class FakeCamera:
    """A webcam that delivers numbered BGR frames until it is closed."""

    def __init__(self, index):
        import numpy as np
        self._np = np
        self.count = 0
        self.closed = False

    def open(self):
        return True

    def read(self):
        if self.closed:
            return None
        self.count += 1
        return self._np.full((48, 64, 3), self.count % 251, dtype=self._np.uint8)

    def close(self):
        self.closed = True


class ScriptedHands:
    """The detector: plays a scripted session of hands (aim, grab, move, snap, scroll, lose the hand)."""

    def __init__(self):
        self.script = ([hand(0.5, 0.5)] * 8 + [hand(0.5 + i * 0.01, 0.5) for i in range(8)]
                       + [hand(0.58, 0.5, gesture="fist")] * 8
                       + [hand(0.58 + i * 0.02, 0.45, gesture="fist") for i in range(8)]
                       + [hand(0.74, 0.45)] * 8
                       + [hand(0.74, 0.45, gesture="two")] * 8 + [hand(0.74, 0.25, gesture="two")] * 20
                       + [None] * 12)
        self.done = threading.Event()

    def __call__(self, image, captured):
        if not self.script:
            self.done.set()
            return ()
        step = self.script.pop(0)
        return () if step is None else (step,)


def run_session(cfg, desk):
    """A whole gesture snapping session on the real tracker and service, with a fake camera and detector."""
    detector = ScriptedHands()
    svc = WindowSnapService(cfg, output_factory=lambda: desk,
                            tracker_factory=lambda camera: HandTracker(
                                camera, hands_factory=lambda: detector, camera_factory=FakeCamera, max_fps=200))
    assert svc.start() == "started"
    assert detector.done.wait(10)
    svc.stop()
    return svc


@pytest.fixture
def desk():
    window = FakeWindow((1000, 100, 3000, 1000))
    window.title = TITLE
    return FakeDesktop(monitors=((0, 0, 1920, 1080), (1920, 0, 3840, 1080)), windows=[window])


def _numbers(desk):
    """Every coordinate, rectangle edge and scroll amount the session produced."""
    found = set()
    for event in desk.events:
        for value in event[1:]:
            if isinstance(value, int):
                found.add(value)
            elif isinstance(value, FakeWindow):
                found.update(value.rect)
    return {n for n in found if abs(n) >= 10}


# --- cloud models ------------------------------------------------------------------------------

def _bridge_cfg(**overrides):
    values = dict(mcps={}, activity_log_share_with_cloud=False, activity_log_enabled=False,
                  codex_share_long_term_memory=False, claude_share_long_term_memory=False)
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshots(cfg):
    from jarvis.claude_bridge.service import claude_tool_snapshot
    from jarvis.codex_bridge.service import codex_tool_snapshot
    return {"codex": codex_tool_snapshot(cfg), "claude": claude_tool_snapshot(cfg)}


def _register(mock_config):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_gesture_tool
    mock_config.gesture_snap_enabled = True
    configure_gesture_tool(mock_config, platform="win32")
    assert "gestureControl" in BUILTIN_TOOLS  # the local model has it


@pytest.mark.parametrize("share_memory", [False, True])
@pytest.mark.parametrize("share_activity", [False, True])
def test_neither_cloud_mode_is_offered_gesture_control_in_any_sharing_setting(mock_config, share_memory,
                                                                            share_activity):
    _register(mock_config)
    cfg = _bridge_cfg(codex_share_long_term_memory=share_memory, claude_share_long_term_memory=share_memory,
                      activity_log_share_with_cloud=share_activity, activity_log_enabled=share_activity)
    for mode, snapshot in _snapshots(cfg).items():
        assert "gestureControl" not in snapshot, mode
        assert "getTime" in snapshot, mode  # one tool withheld, not the catalogue
        assert not any("gesture" in (entry.get("description") or "").casefold() for entry in snapshot.values())


def test_a_cloud_model_asking_for_gesture_control_by_name_is_refused(mock_config):
    from jarvis.bridge.broker import Broker, BrokerLimits
    _register(mock_config)
    snapshot = _snapshots(_bridge_cfg())["claude"]
    broker = Broker(limits=BrokerLimits(deadline_sec=90, queue_limit=1, max_tool_calls=3),
                    validate_args=lambda schema, args: None)
    req = broker.submit_request("turn the camera on", [], "voice", "en",
                                {name: entry["inputSchema"] for name, entry in snapshot.items()})
    assert broker.assign_thread(req.id, "t") and broker.assign_turn(req.id, "u")
    decision = broker.begin_execute(req.id, "call-1", "gestureControl", {"action": "start"}, "t", "u")
    assert decision.kind == "refused"
    assert window_snap_service.get_service() is None or not window_snap_service.get_service().running


def test_a_routine_cannot_start_the_camera_for_a_cloud_model(mock_config):
    from jarvis.routines import definitions, runner
    _register(mock_config)
    snapshot = _snapshots(_bridge_cfg())["codex"]
    routine = definitions.load_routines(
        {"desk": {"steps": [{"tool": "getTime", "args": {}}, {"tool": "gestureControl", "args": {"action": "start"}}]}}
    )["desk"]
    started = []
    svc = SimpleNamespace(running=False, start=lambda: started.append(1) or "started", stop=lambda: True)
    window_snap_service._service = svc
    result = runner.run_routine(routine, mock_config, allowed_tools=frozenset(snapshot), user_print=lambda _l: None)
    assert not result.success and "not available" in result.text
    assert started == []


# --- what the session leaves behind ------------------------------------------------------------

def test_a_whole_session_logs_no_coordinates_measures_or_titles(mock_config, desk, logs, state_file, capsys):
    run_session(mock_config, desk)
    assert desk.of("grab") and desk.of("drop") and desk.of("scroll")  # the session did everything
    out = capsys.readouterr()
    text = "\n".join(logs) + out.out + out.err
    assert logs
    assert TITLE not in text and "Payroll" not in text
    assert not re.search(r"\d+\.\d+", text), "no measures, curls, times or image positions"
    tokens = {int(n) for n in re.findall(r"-?\d+", text)}
    assert not tokens & _numbers(desk), "no pointer positions, window rectangles or scroll amounts"


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND
_watch = {"on": False, "writes": [], "network": []}


def _audit(event, args):
    """Records every Python-level file opened for writing and every network lookup or connection while on."""
    if not _watch["on"]:
        return
    if event == "open":
        path, mode, flags = args
        if (isinstance(mode, str) and any(flag in mode for flag in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS):
            _watch["writes"].append(str(path))
    elif event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"):
        _watch["network"].append(event)


sys.addaudithook(_audit)  # audit hooks cannot be removed; this one does nothing unless a test turns it on


@pytest.fixture
def watched():
    _watch.update(on=True, writes=[], network=[])
    yield _watch
    _watch["on"] = False


def test_a_whole_session_writes_only_the_on_flag(mock_config, desk, state_file, watched):
    run_session(mock_config, desk)
    watched["on"] = False
    assert set(watched["writes"]) == {str(state_file)}


def test_the_on_flag_file_holds_only_the_flag_and_the_time(mock_config, desk, state_file):
    svc = WindowSnapService(mock_config, output_factory=lambda: desk,
                            tracker_factory=lambda camera: SimpleNamespace(
                                subscribe=lambda cb: (lambda: None), start=lambda: True, stop=lambda: None))
    svc.start()
    data = state_file.read_bytes()
    svc.stop()
    assert len(data) == struct.calcsize("<QBd")
    sequence, on, stamp = struct.unpack("<QBd", data)
    assert on == 1 and abs(stamp - time.time()) < 5
    sequence, on, stamp = struct.unpack("<QBd", state_file.read_bytes())
    assert on == 0


def test_a_whole_session_opens_no_network_connection(mock_config, desk, state_file, watched):
    run_session(mock_config, desk)
    watched["on"] = False
    assert watched["network"] == []


def test_failures_with_camera_data_in_their_message_log_only_the_kind_of_error(logs, capsys):
    secret = "x=0.4321 y=0.8765 window=" + TITLE

    detected, delivered = threading.Event(), threading.Event()

    def detector(image, captured):
        detected.set()
        raise ValueError(secret + repr(image))

    def subscriber(hand_frame):
        delivered.set()
        raise RuntimeError(secret)

    for hands_of, ran in ((detector, detected), (lambda image, captured: (hand(0.43, 0.87),), delivered)):
        tracker = HandTracker(0, hands_factory=lambda: hands_of, camera_factory=FakeCamera, max_fps=200)
        tracker.subscribe(subscriber)
        assert tracker.start()
        assert ran.wait(5)
        tracker.stop()
    out = capsys.readouterr()
    text = "\n".join(logs) + out.out + out.err
    assert "ValueError" in text and "RuntimeError" in text
    assert "0.4321" not in text and TITLE not in text and "array" not in text


def test_a_window_that_refuses_to_move_is_reported_without_its_title(mock_config, logs, state_file, capsys):
    window = FakeWindow((1000, 100, 3000, 1000))
    window.title = TITLE
    desk = FakeDesktop(windows=[window])
    desk.fail_window_moves = True
    run_session(mock_config, desk)
    out = capsys.readouterr()
    text = "\n".join(logs) + out.out + out.err
    assert "could not" in text and TITLE not in text


# --- what the tool says ------------------------------------------------------------------------

def test_tool_results_say_the_same_whatever_the_hand_did(mock_config, desk, state_file):
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
    from tests.test_window_snap_service import FakeTracker

    def run(**args):
        return run_tool_with_retries(db=None, cfg=mock_config, tool_name="gestureControl", tool_args=args,
                                     system_prompt="", original_prompt="", redacted_text="", max_retries=1,
                                     quiet=True).reply_text

    BUILTIN_TOOLS["gestureControl"] = GestureControlTool()
    FakeTracker.instances = []
    window_snap_service._service = WindowSnapService(mock_config, output_factory=lambda: desk,
                                                     tracker_factory=lambda camera: FakeTracker(camera))
    replies = [run(action="start"), run(action="status")]
    tracker = FakeTracker.instances[-1]
    t = time.perf_counter()
    for i in range(30):
        tracker.deliver(frame(t + i / 30, hand(0.3 + i * 0.01, 0.6, gesture="fist" if i > 10 else "one")))
    replies += [run(action="status"), run(action="start"), run(action="stop"), run(action="status")]
    assert desk.of("grab")
    assert replies == [
        f"Gesture snapping is on. {window_snap_service.GESTURE_GUIDE}",
        "Gesture snapping is on.", "Gesture snapping is on.", "Gesture snapping is already on.",
        "Gesture snapping is off and the camera is released.", "Gesture snapping is off."]
    assert not any(re.search(r"\d", reply) for reply in replies)


def test_no_log_says_which_hands_the_camera_sees(mock_config, desk, logs, state_file):
    run_session(mock_config, desk)
    text = "\n".join(logs).casefold()
    assert logs
    assert not re.search(r"hands? in view|\b(left|right)\b(?! where)", text)


# --- who can turn the camera on ----------------------------------------------------------------

def _fake_service(mock_config, desk):
    from tests.test_window_snap_service import FakeTracker
    FakeTracker.instances = []
    svc = WindowSnapService(mock_config, output_factory=lambda: desk, tracker_factory=lambda camera: FakeTracker(camera))
    window_snap_service._service = svc
    return svc


@pytest.mark.parametrize("origin, starts", [("phone", False), ("voice", True), ("chat", True), (None, True)])
def test_only_a_request_made_at_the_pc_turns_the_camera_on(mock_config, desk, db, state_file, origin, starts):
    from jarvis.memory.conversation import DialogueMemory
    from jarvis.reply import engine
    _register(mock_config)
    svc = _fake_service(mock_config, desk)
    reply = engine.run_reply_engine(db, mock_config, None, "start gesture control",
                                    DialogueMemory(inactivity_timeout=300.0), quiet=True, origin=origin)
    try:
        assert svc.running is starts
        if not starts:
            assert reply and "at the PC" in reply
    finally:
        svc.stop()


def test_a_phone_can_still_turn_the_camera_off(mock_config, desk, db, state_file):
    from jarvis.memory.conversation import DialogueMemory
    from jarvis.reply import engine
    _register(mock_config)
    svc = _fake_service(mock_config, desk)
    svc.start()
    engine.run_reply_engine(db, mock_config, None, "stop gesture control", DialogueMemory(inactivity_timeout=300.0),
                            quiet=True, origin="phone")
    assert not svc.running


# --- the diagnostic preview --------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="screen capture exclusion is a Windows feature")
def test_the_preview_window_never_appears_in_a_screen_capture():
    pytest.importorskip("PyQt6")
    from PIL import ImageGrab
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from jarvis.gestures import preview

    app = QApplication.instance() or QApplication([])
    import ctypes
    from jarvis.platform.windows import displays

    def settle():
        deadline = time.time() + 0.6
        while time.time() < deadline:
            app.processEvents()

    def captured():
        ratio = window.devicePixelRatioF()
        x, y = int((window.x() + 150) * ratio), int((window.y() + 150) * ratio)
        try:
            with displays.per_monitor_dpi():
                return ImageGrab.grab(bbox=(x, y, x + 1, y + 1), all_screens=True).getpixel((0, 0))[:3]
        except OSError:
            pytest.skip("the screen cannot be captured in this session (locked or no desktop)")

    window = preview.make_window()
    window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    window.setStyleSheet("background: #ff00ff;")
    window.setGeometry(240, 240, 300, 300)
    window.show()
    try:
        settle()
        hwnd = int(window.winId())
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0)  # control: without the exclusion it is captured
        settle()
        if captured() != (255, 0, 255):
            pytest.skip("another window covers the test spot, so the capture cannot be checked")
        window.hide()
        window.show()  # shown again, as the preview is: the exclusion is applied on every show
        settle()
        assert captured() != (255, 0, 255)
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("origin, starts", [("phone", False), (None, True)])
def test_a_phone_cannot_start_the_camera_through_an_approved_confirmation(mock_config, desk, state_file, monkeypatch,
                                                                         origin, starts):
    from jarvis.routines import store
    from jarvis.tools import registry
    from jarvis.tools.confirmation import (ORIGIN_CHAT, SafetyTier, get_confirmation_store, set_dialog_callback,
                                           set_result_handler)
    from jarvis.tools.request_scope import request_scope
    from routine_fakes import FakeTool
    _register(mock_config)
    svc = _fake_service(mock_config, desk)
    monkeypatch.setitem(registry.BUILTIN_TOOLS, "uninstaller", FakeTool("uninstaller", tier=SafetyTier.CONFIRM_DIALOG))
    shown, finished = [], threading.Event()
    set_dialog_callback(lambda request, resolve: shown.append(resolve) or object())
    set_result_handler(lambda reply, success: finished.set(), ORIGIN_CHAT)
    store.load({"desk": {"steps": [{"tool": "uninstaller", "args": {"action": "remove", "target": "Game"}},
                                   {"tool": "gestureControl", "args": {"action": "start"}}]}})
    try:
        with request_scope(origin):
            registry.run_tool_with_retries(None, mock_config, "routineControl", {"action": "run", "name": "desk"},
                                           "", "", "", quiet=True)
        shown[0](True)  # approved in the desktop dialog (or from the phone)
        assert finished.wait(5)
        assert svc.running is starts
    finally:
        svc.stop()
        set_dialog_callback(None)
        set_result_handler(None, ORIGIN_CHAT)
        get_confirmation_store().clear_pending()
        store.reset()
