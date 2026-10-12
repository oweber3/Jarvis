"""The Windows output for gesture snapping: which zone a drop lands in, which windows can be grabbed, and (by
hand, with -m interactive) grabbing and moving a throwaway test window. Default runs never touch
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
    """A stand-in for a slow snap: it runs until released."""

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


def test_a_failed_snap_still_frees_the_window(monkeypatch, window_under_point):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(output, "_snap", explode)
    desk = output.WindowsSnapOutput()
    desk.drop(window_under_point, 10, 10)
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
def test_a_real_test_window_is_grabbed_and_moved():
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
        assert abs(root.winfo_rooty() - (y - 150 + 80)) <= 10
    finally:
        root.destroy()


def _pump(root, seconds=0.6):
    for _ in range(int(seconds / 0.02)):
        root.update()
        time.sleep(0.02)


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_a_maximised_test_window_is_restored_under_the_hand_at_its_normal_size_and_moves():
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
        assert abs(root.winfo_width() - 400) <= 10 and abs(root.winfo_height() - 300) <= 10
        left, top, right, bottom = grabbed.rect
        assert left < x < right and top < y < bottom
        desk.move_window(grabbed, left + 100, top + 60)
        _pump(root)
        assert grabbed.rect == (left + 100, top + 60, right + 100, bottom + 60)
        # The client area sits inside the moved outer frame (borders and title bar).
        assert left + 100 <= root.winfo_rootx() <= left + 100 + 16
        assert top + 60 <= root.winfo_rooty() <= top + 60 + 60
        assert root.state() == "normal"
        assert abs(root.winfo_width() - 400) <= 10 and abs(root.winfo_height() - 300) <= 10
    finally:
        root.destroy()



@pytest.mark.parametrize("kind,buttons", [("click", ["left down", "left up"]),
                                          ("double_click", ["left down", "left up", "left down", "left up"]),
                                          ("right_click", ["right down", "right up"])])
def test_a_click_presses_and_releases_the_button_at_the_pointer(kind, buttons):
    names = {0x0002: "left down", 0x0004: "left up", 0x0008: "right down", 0x0010: "right up"}
    items = output.click_inputs(kind)
    assert [names[i.union.mi.dwFlags] for i in items] == buttons
    assert all(i.type == 0 and i.union.mi.dx == 0 and i.union.mi.dy == 0 for i in items)  # no movement


def test_an_unknown_click_kind_is_refused():
    with pytest.raises(ValueError):
        output.click_inputs("middle_click")


def test_a_click_is_sent_through_the_click_sender():
    sent = []
    output.WindowsSnapOutput(click_sender=sent.append).click("right_click")
    assert sent == ["right_click"]


def test_a_zoom_holds_ctrl_around_the_wheel_and_always_lets_it_go():
    items = output.zoom_inputs(-240)
    ctrl = 0xA3
    assert items[0].type == 1 and items[0].union.ki.wVk == ctrl and not items[0].union.ki.dwFlags & 0x2
    assert items[1].type == 0 and items[1].union.mi.mouseData == (-240) & 0xFFFFFFFF
    assert items[2].type == 1 and items[2].union.ki.wVk == ctrl and items[2].union.ki.dwFlags & 0x2


def test_zooming_sends_the_signed_delta_and_zero_sends_nothing():
    sent = []
    out = output.WindowsSnapOutput(zoom_sender=sent.append)
    out.zoom(120)
    out.zoom(0)
    out.zoom(-120)
    assert sent == [120, -120]


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_real_clicks_and_zoom_reach_the_window_under_the_pointer():
    import ctypes
    import tkinter
    root = tkinter.Tk()
    root.title("gesture click test")
    root.geometry("400x300+300+300")
    root.attributes("-topmost", True)
    seen = []
    root.bind("<Button-1>", lambda e: seen.append("left"))
    root.bind("<Double-Button-1>", lambda e: seen.append("double"))
    root.bind("<Button-3>", lambda e: seen.append("right"))
    root.bind("<Control-MouseWheel>", lambda e: seen.append(("zoom", e.delta)))
    _pump(root, 0.4)
    desk = output.WindowsSnapOutput()
    x, y = root.winfo_rootx() + 200, root.winfo_rooty() + 150
    try:
        desk.move_to(x, y)
        _pump(root, 0.2)
        under = ctypes.windll.user32.GetAncestor(ctypes.windll.user32.WindowFromPoint(output._Point(x, y)), 2)
        if under != ctypes.windll.user32.GetAncestor(root.winfo_id(), 2):
            pytest.skip("another window covers the test window; nothing was clicked")
        desk.click("click")
        _pump(root, 0.6)  # past the double-click time, so the next click is a new one
        desk.click("double_click")
        _pump(root)
        desk.click("right_click")
        _pump(root)
        desk.zoom(120)
        desk.zoom(-120)
        _pump(root)
        assert seen.count("left") >= 2 and "double" in seen and "right" in seen
        assert ("zoom", 120) in seen and ("zoom", -120) in seen
        assert not ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000  # Ctrl was let go
    finally:
        root.destroy()



def test_a_click_follows_swapped_mouse_buttons():
    names = {0x0002: "left down", 0x0004: "left up", 0x0008: "right down", 0x0010: "right up"}
    assert [names[i.union.mi.dwFlags] for i in output.click_inputs("click", swapped=True)] == ["right down",
                                                                                               "right up"]
    assert [names[i.union.mi.dwFlags] for i in output.click_inputs("right_click", swapped=True)] == ["left down",
                                                                                                     "left up"]


def test_a_zoom_uses_the_right_ctrl_key_so_left_ctrl_shortcuts_never_see_a_double_tap():
    items = output.zoom_inputs(120)
    assert items[0].union.ki.wVk == 0xA3 and items[2].union.ki.wVk == 0xA3
    assert items[0].union.ki.dwFlags & 0x1 and items[2].union.ki.dwFlags & 0x1  # extended key
