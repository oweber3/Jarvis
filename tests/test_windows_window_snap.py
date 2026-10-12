"""The Windows output for gesture snapping: which zone a drop lands in, which windows can be grabbed, and (by
hand, with -m interactive) grabbing, moving and putting back a throwaway test window. Default runs never touch
real windows or the pointer."""
import sys
import threading
import time

import pytest

from jarvis.platform.windows import window_snap as output

LEFT, RIGHT = (0, 0, 1280, 1392), (1280, 0, 2560, 1392)
SPACED = ((2576, 16, 2752, 536), (2768, 16, 3152, 536), (3168, 16, 3344, 536))


def test_a_drop_inside_a_zone_lands_in_that_zone():
    assert output.choose_zone(300, 700, (LEFT, RIGHT)) == LEFT
    assert output.choose_zone(1900, 10, (LEFT, RIGHT)) == RIGHT


def test_a_drop_in_the_spacing_between_zones_lands_in_the_nearest_zone():
    assert output.choose_zone(2760, 300, SPACED) == SPACED[1]  # 2 px from the middle zone, 8 from the left
    assert output.choose_zone(3160, 300, SPACED) == SPACED[2]


def test_no_zones_means_no_snap():
    assert output.choose_zone(100, 100, ()) is None


@pytest.mark.parametrize("name", ["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"])
def test_the_desktop_and_taskbar_are_never_grabbed(name):
    assert not output.grabbable_class(name)


def test_application_windows_can_be_grabbed():
    assert output.grabbable_class("Chrome_WidgetWin_1")
    assert output.grabbable_class("Notepad")


def test_scrolling_sends_the_signed_delta_to_the_wheel():
    sent = []
    output.WindowsSnapOutput(wheel_sender=sent.append).scroll(-240)
    output.WindowsSnapOutput(wheel_sender=sent.append).scroll(30)
    assert sent == [-240, 30]


def test_scrolling_by_zero_sends_nothing():
    sent = []
    output.WindowsSnapOutput(wheel_sender=sent.append).scroll(0)
    assert sent == []


def test_a_refused_wheel_event_raises():
    def refuse(delta):
        raise OSError("refused")

    with pytest.raises(OSError):
        output.WindowsSnapOutput(wheel_sender=refuse).scroll(120)


def test_a_wheel_event_is_a_mouse_wheel_input_without_buttons():
    from jarvis.platform.windows.input_control import _Input
    item = output.wheel_input(-120)
    assert isinstance(item, _Input)
    assert item.type == 0
    assert item.union.mi.dwFlags == 0x0800
    assert item.union.mi.mouseData == (-120) & 0xFFFFFFFF
    assert output.wheel_input(120).union.mi.mouseData == 120


MAXIMISED = (-8, -8, 2568, 1400)


def test_a_restored_window_keeps_the_points_share_of_its_width():
    rect = output.restored_rect(1280, 4, MAXIMISED, 800, 600)  # half way across the maximised frame
    left, top, right, bottom = rect
    assert (right - left, bottom - top) == (800, 600)
    assert left == 1280 - 400
    assert top == 4 - output._TITLE_GRIP_PX


def test_a_restored_window_grabbed_at_the_left_edge_starts_at_the_point():
    left, top, right, bottom = output.restored_rect(-8, 10, MAXIMISED, 640, 480)
    assert left == -8 and right == -8 + 640 and top == 10 - output._TITLE_GRIP_PX and bottom == top + 480


class _Hold:
    """A stand-in for a slow snap or put back: it runs until released."""

    def __init__(self):
        self.started, self.release, self.calls = threading.Event(), threading.Event(), []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.started.set()
        assert self.release.wait(5)


@pytest.fixture
def window_under_point(monkeypatch):
    """The point is over window 42; taking it hands back a grab without touching Windows."""
    monkeypatch.setattr(output, "_window_at", lambda x, y: 42)
    monkeypatch.setattr(output, "_take", lambda hwnd, x, y: output.Grabbed(hwnd, (0, 0, 100, 100)))
    return output.Grabbed(42, (0, 0, 100, 100))


def _settle(desk):
    for _ in range(250):
        if desk.grab(10, 10) is not None:
            return True
        time.sleep(0.02)
    return False


def test_a_window_being_snapped_cannot_be_grabbed_until_the_snap_finishes(monkeypatch, window_under_point):
    hold = _Hold()
    monkeypatch.setattr(output, "_snap", hold)
    desk = output.WindowsSnapOutput()
    assert desk.grab(10, 10) is not None
    desk.drop(window_under_point, 10, 10)
    assert hold.started.wait(5)
    assert desk.grab(10, 10) is None
    hold.release.set()
    assert _settle(desk)


def test_a_window_being_put_back_cannot_be_grabbed_until_it_is_back(monkeypatch, window_under_point):
    hold = _Hold()
    monkeypatch.setattr(output, "_restore_placement", hold)
    desk = output.WindowsSnapOutput()
    desk.put_back(window_under_point)
    assert hold.started.wait(5)
    assert desk.grab(10, 10) is None
    hold.release.set()
    assert _settle(desk)


def test_a_failed_snap_still_frees_the_window(monkeypatch, window_under_point):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(output, "_restore_placement", explode)
    desk = output.WindowsSnapOutput()
    desk.put_back(window_under_point)
    assert _settle(desk)


class _FakeDisplays:
    def __init__(self, zones):
        from types import SimpleNamespace
        self.monitor = SimpleNamespace(bounds=(0, 0, 2560, 1440), device="DISPLAY1")
        self.zones, self.languages, self.placed = zones, None, []

    def install(self, monkeypatch):
        from types import SimpleNamespace
        from jarvis.platform.windows import displays, windows_mgmt
        monkeypatch.setattr(displays, "list_monitors", lambda: [self.monitor])

        def zone_sets(monitors, languages=("en",)):
            self.languages = tuple(languages)
            return {"DISPLAY1": SimpleNamespace(rectangles=self.zones)}

        monkeypatch.setattr(displays, "fancyzone_sets", zone_sets)
        monkeypatch.setattr(windows_mgmt, "place_window", lambda hwnd, monitor, zone: self.placed.append((hwnd, zone)))
        return self


def test_a_drop_snaps_into_the_fancyzones_zone_under_the_point(monkeypatch):
    fake = _FakeDisplays((LEFT, RIGHT)).install(monkeypatch)
    output._snap(42, 1900, 300, use_fancyzones=True, languages=("de", "en"))
    assert fake.placed == [(42, RIGHT)]
    assert fake.languages == ("de", "en")


def test_with_fancyzones_switched_off_a_dropped_window_stays_where_it_was_dropped(monkeypatch):
    fake = _FakeDisplays((LEFT, RIGHT)).install(monkeypatch)
    output._snap(42, 1900, 300, use_fancyzones=False, languages=("en",))
    assert fake.placed == []


def test_the_output_snaps_with_its_fancyzones_setting(monkeypatch, window_under_point):
    seen = []
    done = threading.Event()

    def snap(hwnd, x, y, *, use_fancyzones, languages):
        seen.append((use_fancyzones, tuple(languages)))
        done.set()

    monkeypatch.setattr(output, "_snap", snap)
    output.WindowsSnapOutput(use_fancyzones=False, languages=("fr", "en")).drop(window_under_point, 5, 5)
    assert done.wait(5)
    assert seen == [(False, ("fr", "en"))]


def test_the_daemon_builds_the_output_from_the_fancyzones_setting(monkeypatch, window_under_point):
    from types import SimpleNamespace
    from jarvis.gestures import window_snap_service
    from jarvis.tools import registry
    cfg = SimpleNamespace(gesture_snap_enabled=True, windows_fancyzones_enabled=False,
                          fast_commands_locales=["nl"])
    built = {}

    class Service:
        def __init__(self, cfg, output_factory):
            built["output"] = output_factory()

    monkeypatch.setattr(window_snap_service, "WindowSnapService", Service)
    monkeypatch.setattr(window_snap_service, "get_service", lambda: None)
    monkeypatch.setattr(window_snap_service, "set_service", lambda service: None)
    try:
        registry.configure_gesture_tool(cfg, platform="win32")
    finally:
        registry.BUILTIN_TOOLS.pop("gestureControl", None)
    seen, done = [], threading.Event()

    def snap(hwnd, x, y, *, use_fancyzones, languages):
        seen.append((use_fancyzones, tuple(languages)))
        done.set()

    monkeypatch.setattr(output, "_snap", snap)
    built["output"].drop(window_under_point, 5, 5)
    assert done.wait(5)
    assert seen == [(False, ("nl", "en"))]


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_a_real_test_window_is_grabbed_moved_and_put_back():
    import tkinter
    root = tkinter.Tk()
    root.title("gesture snapping test")
    root.geometry("400x300+300+300")
    root.attributes("-topmost", True)
    root.update()
    time.sleep(0.3)
    root.update()
    desk = output.WindowsSnapOutput()
    x, y = root.winfo_rootx() + 200, root.winfo_rooty() + 150
    grabbed = desk.grab(x, y)
    try:
        assert grabbed is not None
        before = grabbed.rect
        desk.move_window(grabbed, before[0] + 120, before[1] + 80)
        for _ in range(20):
            root.update()
            time.sleep(0.02)
        assert abs(root.winfo_rootx() - (x - 200 + 120)) <= 10
        desk.put_back(grabbed)
        for _ in range(20):
            root.update()
            time.sleep(0.02)
        assert abs(root.winfo_rootx() - (x - 200)) <= 10
    finally:
        root.destroy()


def _pump(root, seconds=0.6):
    for _ in range(int(seconds / 0.02)):
        root.update()
        time.sleep(0.02)


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_a_maximised_test_window_is_restored_under_the_hand_and_put_back_maximised_with_its_size_intact():
    import tkinter
    root = tkinter.Tk()
    root.title("gesture snapping maximised test")
    root.geometry("400x300+300+300")
    root.attributes("-topmost", True)
    _pump(root, 0.3)
    root.state("zoomed")
    _pump(root, 0.5)
    desk = output.WindowsSnapOutput()
    x, y = root.winfo_rootx() + root.winfo_width() // 2, root.winfo_rooty() + 5
    try:
        grabbed = desk.grab(x, y)
        assert grabbed is not None
        _pump(root)
        assert root.state() == "normal"
        assert abs(root.winfo_width() - 400) <= 10
        left, top, right, bottom = grabbed.rect
        assert left < x < right and top < y < bottom
        desk.put_back(grabbed)
        _pump(root)
        assert root.state() == "zoomed"
        root.state("normal")
        _pump(root, 0.4)
        assert abs(root.winfo_width() - 400) <= 10 and abs(root.winfo_height() - 300) <= 10
    finally:
        root.destroy()
