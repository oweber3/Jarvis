"""Gesture snapping on synthetic hands and a fake desktop: aiming, a fist grabbing the window under the pointer,
moving it with the hand, snapping on release, cancelling, pinch scrolling, clutch and letting go when the hand is
lost."""
import random
import statistics

import pytest

from jarvis.gestures.window_snap import WindowSnap, WindowSnapSettings
from tests.synthetic_hands import FakeDesktop, FakeWindow, frame, hand

FPS = 30.0
OPEN = 1.0      # fingers apart
PINCHED = 0.1   # thumb on the index tip


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def settings(**overrides):
    """Default thresholds, with smoothing effectively off so geometry is exact unless a test turns it on."""
    values = dict(hand="any", comfort_box=0.5, pinch_start=0.25, pinch_release=0.35, fist_start=0.82,
                  fist_release=0.92, debounce=0.05, scroll_speed=5.0, min_cutoff=1e6, beta=0.0)
    values.update(overrides)
    return WindowSnapSettings(**values)


class Rig:
    """Gesture snapping fed one frame every 1/30 s, with the watchdog clock following the frames."""

    def __init__(self, desk=None, **overrides):
        self.desk = desk or FakeDesktop()
        self.clock = Clock()
        self.snap = WindowSnap(self.desk, settings(**overrides), clock=self.clock)
        self.t = 0.0

    def show(self, *hands, frames=1):
        for _ in range(frames):
            self.t += 1 / FPS
            self.clock.now = self.t
            self.snap.on_frame(frame(self.t, *hands))
            self.snap.tick()

    def wait(self, seconds):
        """Time passes with no frames at all (a frozen or unplugged camera)."""
        end = self.t + seconds
        while self.clock.now < end:
            self.clock.now = min(end, self.clock.now + 0.05)
            self.snap.tick()
        self.t = end

    def glide(self, start, end, *, pinch=OPEN, fist=False, frames=10):
        for i in range(1, frames + 1):
            f = i / frames
            self.show(hand(start[0] + (end[0] - start[0]) * f, start[1] + (end[1] - start[1]) * f, pinch=pinch,
                           fist=fist))

    def grab_at(self, x, y, frames=4):
        """Close the hand into a fist at (x, y); the fingertip drifts as a real curling finger does."""
        self.show(hand(x + 0.01, y + 0.01, fist=True), frames=frames)

    def open_hand(self, x, y):
        self.show(hand(x - 0.02, y + 0.02), frames=3)

    def pinch_at(self, x, y, frames=4):
        self.show(hand(x, y, pinch=0.3))
        self.show(hand(x + 0.01, y + 0.01, pinch=PINCHED), frames=frames)

    def let_go(self, x, y):
        self.show(hand(x, y, pinch=0.3))
        self.show(hand(x - 0.02, y + 0.02, pinch=OPEN), frames=3)


def window(rect=(100, 100, 900, 700), **kw):
    return FakeWindow(rect, **kw)


# --- aiming ----------------------------------------------------------------------------------------

def test_the_pointer_starts_where_it_is_and_follows_the_fingertip():
    rig = Rig(FakeDesktop(at=(500, 400)))
    rig.show(hand(0.3, 0.3))
    assert rig.desk.at == (500, 400)  # no jump when the hand appears
    rig.glide((0.3, 0.3), (0.4, 0.4))
    x, y = rig.desk.at
    assert x > 500 and y > 400


def test_crossing_the_comfort_box_crosses_the_whole_desktop_of_several_monitors():
    monitors = [(-1920, 0, 0, 1080), (0, 0, 2560, 1440), (2560, 0, 4480, 1080)]
    rig = Rig(FakeDesktop(monitors, at=(-1920, 500)), comfort_box=0.5)
    rig.show(hand(0.2, 0.5))
    rig.glide((0.2, 0.5), (0.7, 0.5), frames=20)  # half the camera's width
    assert rig.desk.at[0] == pytest.approx(4479, abs=2)


def test_a_smaller_comfort_box_needs_less_hand_movement():
    def travel(box):
        rig = Rig(FakeDesktop(at=(100, 500)), comfort_box=box)
        rig.show(hand(0.4, 0.5))
        rig.glide((0.4, 0.5), (0.45, 0.5))
        return rig.desk.at[0] - 100
    assert travel(0.25) == pytest.approx(2 * travel(0.5), rel=0.02)


def test_up_and_down_use_the_same_scale_as_left_and_right():
    rig = Rig(FakeDesktop([(0, 0, 3840, 2160)], at=(1000, 1000)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.6, 0.5))
    across = rig.desk.at[0] - 1000
    rig.glide((0.6, 0.5), (0.6, 0.5 + 0.1 * 640 / 480))  # the same distance in camera pixels, downwards
    assert rig.desk.at[1] - 1000 == pytest.approx(across, abs=2)


def test_the_pointer_never_leaves_the_desktop():
    rig = Rig(FakeDesktop([(0, 0, 1920, 1080)], at=(100, 100)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.0, 0.0))
    assert rig.desk.at == (0, 0)
    rig.glide((0.0, 0.0), (1.0, 1.0), frames=20)
    assert rig.desk.at == (1919, 1079)


def test_a_gap_between_monitors_of_different_heights_is_never_a_pointer_position():
    monitors = [(0, 0, 1920, 1080), (1920, 0, 3840, 600)]
    rig = Rig(FakeDesktop(monitors, at=(1800, 1000)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.55, 0.5))
    x, y = rig.desk.at
    assert any(l <= x < r and t <= y < b for l, t, r, b in monitors)


def test_moving_back_from_an_edge_moves_the_pointer_at_once():
    rig = Rig(FakeDesktop(at=(50, 500)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.2, 0.5))
    assert rig.desk.at[0] == 0
    rig.glide((0.2, 0.5), (0.22, 0.5), frames=2)
    assert rig.desk.at[0] > 0


def test_aiming_never_grabs_or_scrolls_anything():
    rig = Rig(FakeDesktop(windows=[window()]))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.6, 0.6))
    assert rig.desk.of("grab") == [] and rig.desk.of("window") == [] and rig.desk.of("scroll") == []


# --- clutch ----------------------------------------------------------------------------------------

def test_an_open_palm_holds_the_pointer_still_and_resuming_does_not_jump():
    rig = Rig(FakeDesktop(at=(900, 500)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.55, 0.5))
    held = rig.desk.at
    for i in range(10):
        rig.show(hand(0.55 - i * 0.03, 0.5, palm=True))
    assert rig.desk.at == held
    rig.show(hand(0.25, 0.5))
    assert rig.desk.at == held
    rig.glide((0.25, 0.5), (0.3, 0.5))
    assert rig.desk.at[0] > held[0]


def test_a_hand_out_of_view_holds_the_pointer_and_its_return_does_not_jump():
    rig = Rig(FakeDesktop(at=(900, 500)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.55, 0.5))
    held = rig.desk.at
    rig.show(frames=15)
    rig.show(hand(0.2, 0.8))
    assert rig.desk.at == held


def test_moving_the_real_mouse_while_clutched_is_respected():
    rig = Rig(FakeDesktop(at=(900, 500)))
    rig.show(hand(0.5, 0.5))
    rig.show(frames=15)
    rig.desk.at = (300, 300)
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.52, 0.5), frames=2)
    assert rig.desk.at[0] > 300 and rig.desk.at[1] == 300


# --- grabbing --------------------------------------------------------------------------------------

def test_a_fist_grabs_the_window_under_where_the_pointer_was_before_the_hand_closed():
    front, behind = window((600, 300, 1200, 800)), window((0, 0, 1920, 1080))
    rig = Rig(FakeDesktop(at=(700, 400), windows=[front, behind]))
    rig.show(hand(0.5, 0.5))
    aim = rig.desk.at
    rig.grab_at(0.5, 0.5)
    assert rig.desk.of("grab") == [("grab", front)]
    assert rig.desk.at == aim


def test_a_fist_shorter_than_the_debounce_grabs_nothing():
    rig = Rig(FakeDesktop(windows=[window()]))
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, fist=True))
    rig.show(hand(0.5, 0.5), frames=5)
    assert rig.desk.of("grab") == []


def test_a_fist_is_never_taken_for_a_pinch_even_with_the_thumb_on_the_index():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, fist=True, pinch=0.1), frames=5)
    assert len(rig.desk.of("grab")) == 1 and rig.snap.state == "holding"


def test_the_aim_stays_still_while_the_hand_closes_and_opens_without_a_gesture():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    before = rig.desk.at
    rig.show(hand(0.53, 0.47, pinch=0.3), frames=3)
    rig.show(hand(0.55, 0.45, pinch=0.28), frames=3)
    assert rig.desk.at == before
    rig.show(hand(0.55, 0.45, pinch=OPEN))
    rig.glide((0.55, 0.45), (0.58, 0.45), frames=3)
    assert rig.desk.at[0] > before[0]


# --- moving and snapping ---------------------------------------------------------------------------

def test_the_held_window_follows_the_hand_keeping_its_place_under_the_pointer():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(at=(700, 400), windows=[win]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.61, 0.51), fist=True)
    x, y = rig.desk.at
    assert x > 1000
    assert (x - win.rect[0], y - win.rect[1]) == (100, 100)


def test_opening_the_fist_snaps_the_window_where_the_pointer_is():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(at=(700, 400), windows=[win]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.61, 0.51), fist=True)
    held_at = rig.desk.at
    rig.open_hand(0.61, 0.51)
    assert rig.desk.of("drop") == [("drop", win, *held_at)]  # the finger straightening does not shift the drop


def test_a_maximised_window_is_restored_and_stays_under_the_pointer():
    win = window((0, 0, 1920, 1080), maximised=True, restored=(200, 200, 1000, 800))
    rig = Rig(FakeDesktop(at=(960, 300), windows=[win]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    assert not win.maximised
    rig.glide((0.51, 0.51), (0.55, 0.51), fist=True, frames=3)
    left, top, right, bottom = win.rect
    assert left <= rig.desk.at[0] < right and top <= rig.desk.at[1] < bottom


def test_a_fist_over_no_window_holds_the_aim_still_and_drops_nothing():
    rig = Rig(FakeDesktop(at=(700, 400), windows=[]))
    rig.show(hand(0.5, 0.5))
    aim = rig.desk.at
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.6, 0.51), fist=True, frames=3)
    assert rig.desk.at == aim
    rig.open_hand(0.6, 0.51)
    assert rig.desk.of("window") == [] and rig.desk.of("drop") == []


def test_opening_the_hand_flat_snaps_the_window_too():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(at=(700, 400), windows=[win]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.61, 0.51), fist=True)
    held_at = rig.desk.at
    rig.show(hand(0.63, 0.53, palm=True), frames=3)
    assert rig.desk.of("drop") == [("drop", win, *held_at)]


def test_a_window_that_refuses_to_move_is_let_go_and_tracking_continues():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(at=(700, 400), windows=[win]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    rig.desk.fail_window_moves = True
    rig.glide((0.51, 0.51), (0.61, 0.51), fist=True, frames=4)
    rig.desk.fail_window_moves = False
    rig.open_hand(0.61, 0.51)
    before = rig.desk.at
    rig.glide((0.59, 0.53), (0.5, 0.53), frames=3)
    assert rig.desk.of("drop") == [] and rig.desk.at[0] < before[0]


# --- scrolling -------------------------------------------------------------------------------------

def test_a_pinch_moved_up_scrolls_the_content_up_and_holds_the_pointer_still():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    aim = rig.desk.at
    rig.pinch_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.41), pinch=PINCHED)  # a tenth of the camera's height, upwards
    assert rig.desk.at == aim
    assert rig.desk.scrolled() == pytest.approx(-5 * 120, abs=30)  # content up: the wheel turns towards the user


def test_a_pinch_moved_down_scrolls_the_other_way():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.56), pinch=PINCHED)
    assert rig.desk.scrolled() == pytest.approx(5 * 120 / 2, abs=30)


def test_the_scroll_speed_setting_scales_the_scroll():
    def amount(speed):
        rig = Rig(FakeDesktop(at=(700, 400)), scroll_speed=speed)
        rig.show(hand(0.5, 0.5))
        rig.pinch_at(0.5, 0.5)
        rig.glide((0.51, 0.51), (0.51, 0.41), pinch=PINCHED)
        return abs(rig.desk.scrolled())
    assert amount(10) == pytest.approx(2 * amount(5), rel=0.1)


def test_slow_movement_scrolls_in_small_steps_not_a_flood_of_events():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.49), pinch=PINCHED, frames=30)
    steps = [e[1] for e in rig.desk.of("scroll")]
    assert steps and all(abs(step) >= 30 for step in steps) and len(steps) < 30


def test_a_pinch_with_the_index_slightly_bent_still_scrolls():
    """Measured on a real webcam: a natural pinch bends the index to a curl of 0.93 to 1.0."""
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, pinch=0.1, curl=0.95), frames=4)
    assert rig.snap.state == "scrolling"


def test_a_brief_dip_towards_a_fist_does_not_end_a_scroll():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    rig.show(hand(0.51, 0.51, pinch=PINCHED, curl=0.78))
    rig.show(hand(0.51, 0.51, pinch=PINCHED, curl=0.95), frames=3)
    assert rig.snap.state == "scrolling"


def test_a_pinch_that_closes_into_a_fist_grabs():
    rig = Rig(FakeDesktop(at=(700, 400), windows=[window((0, 0, 1920, 1080))]))
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    rig.show(hand(0.51, 0.51, fist=True), frames=5)
    assert rig.snap.state == "holding"


def test_sideways_movement_does_not_scroll():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.61, 0.51), pinch=PINCHED)
    assert rig.desk.of("scroll") == []


def test_letting_go_of_the_pinch_ends_the_scroll_and_aiming_resumes():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.45), pinch=PINCHED)
    rig.let_go(0.51, 0.45)
    count = len(rig.desk.of("scroll"))
    rig.glide((0.49, 0.47), (0.49, 0.37))
    assert len(rig.desk.of("scroll")) == count and rig.snap.state == "aiming"


def test_a_side_on_pinch_scrolls_even_when_the_trackers_depth_is_off():
    """Measured on a real webcam: side-on, the tips touch in the image while the 3D estimate keeps them apart."""
    rig = Rig()
    rig.show(hand(0.5, 0.5, pinch=0.6, depth_error=0.03))
    rig.show(hand(0.5, 0.5, pinch=0.15, depth_error=0.03), frames=4)
    assert rig.snap.state == "scrolling"


def test_a_ratio_hovering_between_the_thresholds_keeps_scrolling():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    for ratio in (0.3, 0.33, 0.27, 0.34, 0.3):
        rig.show(hand(0.51, 0.51, pinch=ratio))
    assert rig.snap.state == "scrolling"
    rig.show(hand(0.51, 0.51, pinch=OPEN), frames=3)
    assert rig.snap.state == "aiming"


def test_a_window_that_refuses_scrolling_does_not_break_tracking():
    rig = Rig(FakeDesktop(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5)
    rig.desk.fail = True
    rig.glide((0.51, 0.51), (0.51, 0.45), pinch=PINCHED)
    rig.desk.fail = False
    rig.glide((0.51, 0.45), (0.51, 0.40), pinch=PINCHED)
    assert rig.desk.of("scroll")


# --- losing the hand and stopping ------------------------------------------------------------------

def test_losing_the_hand_while_holding_leaves_the_window_where_it_is():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(at=(700, 400), windows=[win]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.61, 0.51), fist=True)
    moved = win.rect
    rig.show(frames=10)
    rig.show(hand(0.4, 0.4), frames=3)
    assert win.rect == moved and rig.desk.of("drop") == []
    assert rig.snap.state != "holding"


def test_a_camera_that_stops_while_holding_lets_the_window_go():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    assert rig.snap.state == "holding"
    rig.wait(1.0)
    assert rig.snap.state == "paused"


def test_a_momentary_tracking_dropout_keeps_holding():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    rig.show(frames=2)
    rig.show(hand(0.51, 0.51, fist=True))
    assert rig.snap.state == "holding"


def test_slow_detection_does_not_let_go():
    """Frames arrive late (captured long before delivery) but steadily: the hand is not lost."""
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    for _ in range(10):
        rig.t += 1 / FPS
        rig.clock.now = rig.t + 0.4
        rig.snap.on_frame(frame(rig.t, hand(0.51, 0.51, fist=True)))
        rig.snap.tick()
    assert rig.snap.state == "holding"


def test_closing_while_holding_leaves_the_window_and_later_frames_do_nothing():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(at=(700, 400), windows=[win]))
    rig.show(hand(0.5, 0.5))
    rig.grab_at(0.5, 0.5)
    rig.snap.close()
    count = len(rig.desk.events)
    rig.glide((0.51, 0.51), (0.61, 0.51), fist=True)
    rig.open_hand(0.61, 0.51)
    assert len(rig.desk.events) == count and rig.desk.of("drop") == []


def test_a_pointer_that_refuses_input_does_not_break_tracking():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.desk.fail = True
    rig.glide((0.5, 0.5), (0.6, 0.5))
    rig.desk.fail = False
    rig.glide((0.6, 0.5), (0.65, 0.5))
    assert rig.desk.of("pointer")


# --- which hand ------------------------------------------------------------------------------------

def test_only_the_chosen_hand_moves_the_pointer():
    rig = Rig(FakeDesktop(at=(900, 500)), hand="right")
    for i in range(10):
        rig.show(hand(0.3 + i * 0.01, 0.5, side="left"))
    assert rig.desk.of("pointer") == []


def test_with_any_hand_the_hand_in_use_keeps_control_when_another_appears():
    rig = Rig(FakeDesktop(at=(900, 500)))
    rig.show(hand(0.5, 0.5, side="left", score=0.7))
    for i in range(1, 6):
        rig.show(hand(0.5 + i * 0.01, 0.5, side="left", score=0.7),
                 hand(0.8, 0.2 + i * 0.05, side="right", score=0.99))
    x, y = rig.desk.at
    assert x > 900 and y == 500


def test_another_hand_never_takes_over_during_a_dropout():
    rig = Rig(FakeDesktop(at=(900, 500), windows=[window((0, 0, 1920, 1080))]))
    rig.show(hand(0.5, 0.5, side="right"))
    rig.grab_at(0.5, 0.5)
    rig.show(hand(0.2, 0.2, palm=True, side="left"))  # the right hand missed for one frame
    rig.show(hand(0.51, 0.51, fist=True, side="right"), hand(0.2, 0.2, palm=True, side="left"), frames=5)
    assert rig.snap.state == "holding" and rig.desk.of("drop") == []


def test_two_hands_with_the_same_label_do_not_make_the_pointer_jump():
    rig = Rig(FakeDesktop(at=(900, 500)))
    a, b = (0.3, 0.5), (0.7, 0.5)
    rig.show(hand(*a))
    for i in range(6):
        rig.show(*((hand(*a), hand(*b)) if i % 2 else (hand(*b), hand(*a))))
    assert rig.desk.of("pointer") == []


# --- smoothing -------------------------------------------------------------------------------------

def test_smoothing_steadies_a_jittery_fingertip():
    def spread(**overrides):
        rng = random.Random(7)
        rig = Rig(FakeDesktop(at=(900, 500)), **overrides)
        xs = []
        for _ in range(150):
            rig.show(hand(0.5 + rng.uniform(-0.003, 0.003), 0.5 + rng.uniform(-0.003, 0.003)))
            xs.append(rig.desk.at[0])
        return statistics.pstdev(xs[30:])
    assert spread(min_cutoff=1.0, beta=0.005) < spread() / 2


def test_smoothing_still_follows_a_deliberate_movement():
    rig = Rig(FakeDesktop(at=(200, 500)), min_cutoff=1.0, beta=0.005)
    rig.show(hand(0.4, 0.5))
    rig.glide((0.4, 0.5), (0.5, 0.5), frames=6)
    rig.show(hand(0.5, 0.5), frames=30)
    assert rig.desk.at[0] == pytest.approx(200 + 0.1 * 640 * 1920 / 320, abs=10)


# --- state and settings ----------------------------------------------------------------------------

def test_the_state_names_what_the_hand_is_doing():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    assert rig.snap.state == "paused"
    rig.show(hand(0.5, 0.5))
    assert rig.snap.state == "aiming"
    rig.show(hand(0.5, 0.5, pinch=0.3))
    assert rig.snap.state == "hand closing"
    rig.show(hand(0.5, 0.5, pinch=PINCHED), frames=3)
    assert rig.snap.state == "scrolling"
    rig.show(hand(0.5, 0.5, fist=True), frames=3)
    assert rig.snap.state == "holding"
    empty = Rig(FakeDesktop(windows=[]))
    empty.show(hand(0.5, 0.5))
    empty.grab_at(0.5, 0.5)
    assert empty.snap.state == "holding nothing"


def test_settings_come_from_the_config_with_each_release_above_its_start(mock_config):
    mock_config.gesture_snap_pinch_start = 0.3
    mock_config.gesture_snap_pinch_release = 0.2
    mock_config.gesture_snap_fist_start = 1.1
    mock_config.gesture_snap_fist_release = 0.9
    loaded = WindowSnapSettings.from_config(mock_config)
    assert loaded.pinch_release > loaded.pinch_start
    assert loaded.fist_release > loaded.fist_start
