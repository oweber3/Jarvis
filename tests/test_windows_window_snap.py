"""The Windows output for gesture snapping: which zone a drop lands in, which windows can be grabbed, and (by
hand, with -m interactive) grabbing, moving and putting back a throwaway test window. Default runs never touch
real windows or the pointer."""
import sys
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
