"""Gesture snapping on synthetic hands and a fake desktop, by raised fingers: one finger aims, a fist grabs the
window under the pointer and moves it, raising a finger snaps it, two fingers scroll, an open hand pauses."""
import random
import statistics

import pytest

from jarvis.gestures.window_snap import WindowSnap, WindowSnapSettings, finger_curl
from tests.synthetic_hands import FakeDesktop, FakeWindow, frame, hand

FPS = 30.0
SETTLE = 5  # frames for a gesture to outlast the 100 ms debounce


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def settings(**overrides):
    """Default thresholds, with smoothing effectively off so geometry is exact unless a test turns it on."""
    values = dict(hand="any", comfort_box=0.5, finger_up=1.1, finger_down=0.95, debounce=0.1, scroll_speed=5.0,
                  min_cutoff=1e6, beta=0.0)
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

    def start(self, x=0.5, y=0.5):
        """Raise one finger until it counts."""
        self.show(hand(x, y), frames=SETTLE)

    def glide(self, start, end, *, gesture="one", frames=10):
        for i in range(1, frames + 1):
            f = i / frames
            self.show(hand(start[0] + (end[0] - start[0]) * f, start[1] + (end[1] - start[1]) * f,
                           gesture=gesture))

    def grab_at(self, x, y):
        """Close the hand into a fist at (x, y); the palm shifts slightly as a real closing hand does."""
        self.show(hand(x + 0.005, y + 0.005, gesture="fist"), frames=SETTLE)

    def two_at(self, x, y):
        self.show(hand(x + 0.01, y + 0.01, gesture="two"), frames=SETTLE)

    def raise_finger(self, x, y):
        self.show(hand(x - 0.005, y - 0.005), frames=SETTLE)


def window(rect=(100, 100, 900, 700), **kw):
    return FakeWindow(rect, **kw)


# --- recognising fingers ---------------------------------------------------------------------------

def test_finger_curl_tells_a_raised_finger_from_a_folded_one():
    h = hand(gesture="one")
    assert finger_curl(h, "INDEX") > 1.1 and finger_curl(h, "MIDDLE") < 0.95


def test_a_finger_between_the_thresholds_keeps_its_last_state():
    rig = Rig()
    rig.start()
    rig.show(hand(0.5, 0.5, curls={"middle": 1.0}), frames=SETTLE)  # middle half raised: still one finger
    assert rig.snap.state == "aiming"
    rig.show(hand(0.5, 0.5, gesture="two"), frames=SETTLE)
    rig.show(hand(0.5, 0.5, gesture="two", curls={"middle": 1.0}), frames=SETTLE)  # half folded: still two
    assert rig.snap.state == "scrolling"


def test_a_gesture_shorter_than_the_debounce_changes_nothing():
    rig = Rig(FakeDesktop(windows=[window()]))
    rig.start()
    rig.show(hand(0.5, 0.5, gesture="fist"), frames=2)
    rig.show(hand(0.5, 0.5), frames=SETTLE)
    assert rig.desk.of("grab") == [] and rig.snap.state == "aiming"


def test_an_unknown_finger_combination_holds_the_pointer_still():
    rig = Rig(FakeDesktop())
    rig.start()
    before = rig.desk.at
    rig.glide((0.5, 0.5), (0.6, 0.6), gesture="rock")
    assert rig.desk.at == before and rig.desk.of("grab") == [] and rig.desk.of("scroll") == []


# --- aiming ----------------------------------------------------------------------------------------

THREE = [(-1920, 0, 0, 1080), (0, 0, 2560, 1440), (2560, 0, 4480, 1080)]  # desktop centre (1280, 720)


def test_raising_the_hand_anywhere_puts_the_pointer_in_the_middle_of_the_desktop():
    for place in ((0.5, 0.5), (0.3, 0.65)):
        rig = Rig(FakeDesktop(THREE, at=(-1500, 100)))
        rig.start(*place)
        assert rig.desk.at == (1280, 720)


def test_taking_the_hand_out_of_view_and_raising_it_elsewhere_re_centres():
    rig = Rig(FakeDesktop([(0, 0, 1920, 1080)]))
    rig.start(0.5, 0.5)
    rig.glide((0.5, 0.5), (0.6, 0.5))
    assert rig.desk.at[0] > 960
    rig.show(frames=15)
    rig.start(0.3, 0.5)
    assert rig.desk.at == (960, 540)


def test_the_comfort_box_edges_are_the_desktop_edges_across_several_monitors():
    rig = Rig(FakeDesktop(THREE), comfort_box=0.5)
    rig.start(0.5, 0.5)
    rig.glide((0.5, 0.5), (0.25, 0.5), frames=10)  # the left edge of a box half the camera's width
    assert rig.desk.at[0] == -1920
    rig.glide((0.25, 0.5), (0.75, 0.5), frames=20)
    assert rig.desk.at[0] == pytest.approx(4479, abs=2)


def test_a_palm_outside_the_comfort_box_holds_the_pointer_at_the_edge():
    rig = Rig(FakeDesktop([(0, 0, 1920, 1080)]))
    rig.start(0.5, 0.5)
    rig.glide((0.5, 0.5), (0.05, 0.5), frames=10)
    assert rig.desk.at[0] == 0
    rig.glide((0.05, 0.5), (0.95, 0.5), frames=20)
    assert rig.desk.at[0] == 1919


def test_a_smaller_comfort_box_needs_less_hand_movement():
    def travel(box):
        rig = Rig(FakeDesktop(), comfort_box=box)
        rig.start(0.5, 0.5)
        start = rig.desk.at[0]
        rig.glide((0.5, 0.5), (0.53, 0.5))
        return rig.desk.at[0] - start
    assert travel(0.25) == pytest.approx(2 * travel(0.5), rel=0.02)


def test_up_and_down_use_the_same_scale_as_left_and_right():
    rig = Rig(FakeDesktop([(0, 0, 3840, 2160)]))
    rig.start()
    x0, y0 = rig.desk.at
    rig.glide((0.5, 0.5), (0.55, 0.5))
    across = rig.desk.at[0] - x0
    rig.glide((0.55, 0.5), (0.55, 0.5 + 0.05 * 640 / 480))  # the same distance in camera pixels, downwards
    assert rig.desk.at[1] - y0 == pytest.approx(across, abs=2)


def test_the_pointer_never_leaves_the_desktop():
    rig = Rig(FakeDesktop([(0, 0, 1920, 1080)]))
    rig.start()
    rig.glide((0.5, 0.5), (0.0, 0.0))
    assert rig.desk.at == (0, 0)
    rig.glide((0.0, 0.0), (1.0, 1.0), frames=20)
    assert rig.desk.at == (1919, 1079)


def test_a_gap_between_monitors_of_different_heights_is_never_a_pointer_position():
    monitors = [(0, 0, 1920, 1080), (1920, 0, 3840, 600)]
    rig = Rig(FakeDesktop(monitors))
    rig.start(0.6, 0.7)
    x, y = rig.desk.at
    assert any(l <= x < r and t <= y < b for l, t, r, b in monitors)


def test_folding_and_raising_fingers_does_not_move_the_pointer():
    rig = Rig(FakeDesktop())
    rig.start()
    aim = rig.desk.at
    for gesture in ("two", "one", "rock", "one"):
        rig.show(hand(0.5, 0.5, gesture=gesture), frames=SETTLE)
    assert rig.desk.at == aim


def test_aiming_never_grabs_or_scrolls_anything():
    rig = Rig(FakeDesktop(windows=[window()]))
    rig.start()
    rig.glide((0.5, 0.5), (0.6, 0.6))
    assert rig.desk.of("grab") == [] and rig.desk.of("window") == [] and rig.desk.of("scroll") == []


# --- pausing ---------------------------------------------------------------------------------------

def test_an_open_hand_holds_the_pointer_still_and_one_finger_brings_it_to_the_hand():
    rig = Rig(FakeDesktop([(0, 0, 1920, 1080)]))
    rig.start()
    rig.glide((0.5, 0.5), (0.55, 0.5))
    held = rig.desk.at
    rig.glide((0.55, 0.5), (0.45, 0.5), gesture="open", frames=10)
    assert rig.desk.at == held
    rig.start(0.45, 0.5)
    assert rig.desk.at[0] < held[0]


def test_a_hand_out_of_view_holds_the_pointer_still():
    rig = Rig(FakeDesktop([(0, 0, 1920, 1080)]))
    rig.start()
    rig.glide((0.5, 0.5), (0.55, 0.5))
    held = rig.desk.at
    rig.show(frames=15)
    assert rig.desk.at == held


# --- grabbing, moving and snapping -----------------------------------------------------------------

def test_a_fist_grabs_the_window_under_where_the_pointer_was_before_the_hand_closed():
    front, behind = window((600, 300, 1200, 800)), window((0, 0, 1920, 1080))
    rig = Rig(FakeDesktop(windows=[front, behind]))
    rig.start()
    aim = rig.desk.at
    rig.grab_at(0.5, 0.5)
    assert rig.desk.of("grab") == [("grab", front)]
    assert abs(rig.desk.at[0] - aim[0]) < 10 and abs(rig.desk.at[1] - aim[1]) < 10  # easing towards the palm


def test_a_fist_raised_from_the_clutch_grabs_the_window_under_the_pointer():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.show(hand(0.5, 0.5, gesture="fist"), frames=SETTLE)
    assert rig.desk.of("grab") == [("grab", win)] and rig.snap.state == "holding"


def test_the_held_window_follows_the_hand_keeping_its_place_under_the_pointer():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.glide((0.5, 0.5), (0.55, 0.5), gesture="fist")
    x, y = rig.desk.at
    assert x > 1100
    assert (x - win.rect[0], y - win.rect[1]) == (360, 240)  # where the pointer was on the window at the grab


def test_raising_a_finger_snaps_the_window_where_the_pointer_is():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.53), (0.61, 0.53), gesture="fist")
    held_at = rig.desk.at
    rig.raise_finger(0.61, 0.53)
    assert rig.desk.of("drop") == [("drop", win, *held_at)]  # the finger rising does not shift the drop
    assert rig.snap.state == "aiming"


@pytest.mark.parametrize("gesture", ["two", "open"])
def test_two_fingers_or_an_open_hand_also_snap_the_window(gesture):
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.53), (0.61, 0.53), gesture="fist")
    held_at = rig.desk.at
    rig.show(hand(0.6, 0.5, gesture=gesture), frames=SETTLE)
    assert rig.desk.of("drop") == [("drop", win, *held_at)]


def test_a_loosening_little_finger_keeps_holding():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.show(hand(0.51, 0.53, gesture="fist", curls={"pinky": 1.3}), frames=SETTLE)
    assert rig.snap.state == "holding" and rig.desk.of("drop") == []


def test_a_maximised_window_is_restored_and_stays_under_the_pointer():
    win = window((0, 0, 1920, 1080), maximised=True, restored=(200, 200, 1000, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    assert not win.maximised
    rig.glide((0.51, 0.53), (0.55, 0.53), gesture="fist", frames=3)
    left, top, right, bottom = win.rect
    assert left <= rig.desk.at[0] < right and top <= rig.desk.at[1] < bottom


def test_a_fist_over_no_window_holds_the_aim_still_and_drops_nothing():
    rig = Rig(FakeDesktop(windows=[]))
    rig.start()
    aim = rig.desk.at
    rig.grab_at(0.5, 0.5)
    assert rig.snap.state == "holding nothing"
    rig.glide((0.51, 0.53), (0.6, 0.53), gesture="fist", frames=3)
    assert rig.desk.at == aim
    rig.raise_finger(0.6, 0.53)
    assert rig.desk.of("window") == [] and rig.desk.of("drop") == []


def test_a_window_that_refuses_to_move_is_let_go_and_tracking_continues():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.desk.fail_window_moves = True
    rig.glide((0.51, 0.53), (0.61, 0.53), gesture="fist", frames=4)
    rig.desk.fail_window_moves = False
    rig.raise_finger(0.61, 0.53)
    before = rig.desk.at
    rig.glide((0.6, 0.53), (0.5, 0.53), frames=3)
    assert rig.desk.of("drop") == [] and rig.desk.at[0] < before[0]


# --- scrolling -------------------------------------------------------------------------------------

def test_two_fingers_moved_up_scroll_the_content_up_and_hold_the_pointer_still():
    rig = Rig(FakeDesktop())
    rig.start()
    aim = rig.desk.at
    rig.two_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.41), gesture="two")  # a tenth of the camera's height, upwards
    assert rig.desk.at == aim
    assert rig.desk.scrolled() == pytest.approx(-5 * 120, abs=30)  # content up: the wheel turns towards the user


def test_two_fingers_moved_down_scroll_the_other_way():
    rig = Rig(FakeDesktop())
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.56), gesture="two")
    assert rig.desk.scrolled() == pytest.approx(5 * 120 / 2, abs=30)


def test_the_scroll_speed_setting_scales_the_scroll():
    def amount(speed):
        rig = Rig(FakeDesktop(), scroll_speed=speed)
        rig.start()
        rig.two_at(0.5, 0.5)
        rig.glide((0.51, 0.51), (0.51, 0.41), gesture="two")
        return abs(rig.desk.scrolled())
    assert amount(10) == pytest.approx(2 * amount(5), rel=0.1)


def test_slow_movement_scrolls_in_small_steps_not_a_flood_of_events():
    rig = Rig(FakeDesktop())
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.49), gesture="two", frames=30)
    steps = [e[1] for e in rig.desk.of("scroll")]
    assert steps and all(abs(step) >= 30 for step in steps) and len(steps) < 30


def test_sideways_movement_does_not_scroll():
    rig = Rig(FakeDesktop())
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.61, 0.51), gesture="two")
    assert rig.desk.of("scroll") == []


def test_one_finger_ends_the_scroll_and_aiming_resumes():
    rig = Rig(FakeDesktop())
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.glide((0.51, 0.51), (0.51, 0.45), gesture="two")
    rig.show(hand(0.51, 0.45), frames=9)
    count = len(rig.desk.of("scroll"))
    rig.glide((0.51, 0.45), (0.51, 0.35))
    assert len(rig.desk.of("scroll")) == count and rig.snap.state == "aiming"


def test_closing_the_hand_from_two_fingers_ends_the_scroll_and_grabs_nothing():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.show(hand(0.51, 0.51, gesture="fist"), frames=12)
    assert rig.desk.of("grab") == [] and rig.snap.state != "scrolling"
    rig.start(0.51, 0.51)  # point first, then a fist grabs
    rig.grab_at(0.51, 0.51)
    assert rig.snap.state == "holding"


def test_fingers_bending_briefly_while_scrolling_do_not_end_the_scroll():
    """Measured on a real webcam: moving the hand bends the two raised fingers to about 0.92 to 1.0."""
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.show(hand(0.51, 0.51, gesture="two", curls={"index": 0.9, "middle": 0.9}), frames=6)  # 0.2 s
    rig.show(hand(0.51, 0.51, gesture="two"), frames=3)
    assert rig.snap.state == "scrolling" and rig.desk.of("grab") == []


def test_one_finger_held_long_enough_ends_the_scroll():
    rig = Rig(FakeDesktop())
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.show(hand(0.51, 0.51), frames=9)  # 0.3 s
    assert rig.snap.state == "aiming"


def test_a_window_that_refuses_scrolling_does_not_break_tracking():
    rig = Rig(FakeDesktop())
    rig.start()
    rig.two_at(0.5, 0.5)
    rig.desk.fail = True
    rig.glide((0.51, 0.51), (0.51, 0.45), gesture="two")
    rig.desk.fail = False
    rig.glide((0.51, 0.45), (0.51, 0.40), gesture="two")
    assert rig.desk.of("scroll")


# --- losing the hand and stopping ------------------------------------------------------------------

def test_losing_the_hand_while_holding_leaves_the_window_where_it_is():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.glide((0.51, 0.53), (0.61, 0.53), gesture="fist")
    moved = win.rect
    rig.show(frames=10)
    rig.start(0.4, 0.4)
    assert win.rect == moved and rig.desk.of("drop") == []
    assert rig.snap.state == "aiming"


def test_a_camera_that_stops_while_holding_lets_the_window_go():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    assert rig.snap.state == "holding"
    rig.wait(1.0)
    assert rig.snap.state == "paused"


def test_a_momentary_tracking_dropout_keeps_holding():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.show(frames=2)
    rig.show(hand(0.51, 0.53, gesture="fist"))
    assert rig.snap.state == "holding"


def test_slow_detection_does_not_let_go():
    """Frames arrive late (captured long before delivery) but steadily: the hand is not lost."""
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    for _ in range(10):
        rig.t += 1 / FPS
        rig.clock.now = rig.t + 0.4
        rig.snap.on_frame(frame(rig.t, hand(0.51, 0.53, gesture="fist")))
        rig.snap.tick()
    assert rig.snap.state == "holding"


def test_closing_while_holding_leaves_the_window_and_later_frames_do_nothing():
    win = window((600, 300, 1200, 800))
    rig = Rig(FakeDesktop(windows=[win]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.snap.close()
    count = len(rig.desk.events)
    rig.glide((0.51, 0.53), (0.61, 0.53), gesture="fist")
    rig.raise_finger(0.61, 0.53)
    assert len(rig.desk.events) == count and rig.desk.of("drop") == []


def test_a_pointer_that_refuses_input_does_not_break_tracking():
    rig = Rig()
    rig.start()
    rig.desk.fail = True
    rig.glide((0.5, 0.5), (0.6, 0.5))
    rig.desk.fail = False
    rig.glide((0.6, 0.5), (0.65, 0.5))
    assert rig.desk.of("pointer")


# --- which hand ------------------------------------------------------------------------------------

def test_only_the_chosen_hand_moves_the_pointer():
    rig = Rig(FakeDesktop(), hand="right")
    for i in range(10):
        rig.show(hand(0.3 + i * 0.01, 0.5, side="left"))
    assert rig.desk.of("pointer") == []


def test_with_any_hand_the_hand_in_use_keeps_control_when_another_appears():
    rig = Rig(FakeDesktop())
    rig.show(hand(0.5, 0.5, side="left", score=0.7), frames=SETTLE)
    for i in range(1, 6):
        rig.show(hand(0.5 + i * 0.01, 0.5, side="left", score=0.7),
                 hand(0.8, 0.2 + i * 0.05, side="right", score=0.99))
    x, y = rig.desk.at
    assert x > 960 and y == 540


def test_another_hand_never_takes_over_during_a_dropout():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    rig.show(hand(0.5, 0.5, side="right"), frames=SETTLE)
    rig.grab_at(0.5, 0.5)
    rig.show(hand(0.2, 0.2, gesture="open", side="left"))  # the right hand missed for one frame
    rig.show(hand(0.51, 0.53, gesture="fist", side="right"), hand(0.2, 0.2, gesture="open", side="left"),
             frames=SETTLE)
    assert rig.snap.state == "holding" and rig.desk.of("drop") == []


def test_two_hands_with_the_same_label_do_not_make_the_pointer_jump():
    rig = Rig(FakeDesktop())
    a, b = (0.4, 0.5), (0.6, 0.5)
    rig.show(hand(*a), frames=SETTLE)
    aim = rig.desk.at
    for i in range(6):
        rig.show(*((hand(*a), hand(*b)) if i % 2 else (hand(*b), hand(*a))))
    assert rig.desk.at == aim


# --- smoothing -------------------------------------------------------------------------------------

def test_smoothing_steadies_a_jittery_fingertip():
    def spread(**overrides):
        rng = random.Random(7)
        rig = Rig(FakeDesktop(), **overrides)
        xs = []
        for _ in range(150):
            rig.show(hand(0.5 + rng.uniform(-0.003, 0.003), 0.5 + rng.uniform(-0.003, 0.003)))
            xs.append(rig.desk.at[0])
        return statistics.pstdev(xs[30:])
    assert spread(min_cutoff=1.0, beta=0.005) < spread() / 2


def test_smoothing_still_follows_a_deliberate_movement():
    rig = Rig(FakeDesktop(), min_cutoff=1.0, beta=0.005)
    rig.start(0.4, 0.5)
    rig.glide((0.4, 0.5), (0.5, 0.5), frames=6)
    rig.show(hand(0.5, 0.5), frames=30)
    assert rig.desk.at[0] == pytest.approx(960 + 0.1 * 640 * 1920 / 320, abs=10)


# --- state and settings ----------------------------------------------------------------------------

def test_the_state_names_what_the_hand_is_doing():
    rig = Rig(FakeDesktop(windows=[window((0, 0, 1920, 1080))]))
    assert rig.snap.state == "paused"
    rig.start()
    assert rig.snap.state == "aiming"
    rig.show(hand(0.5, 0.5, gesture="two"))
    assert rig.snap.state == "hand changing"
    rig.show(hand(0.5, 0.5, gesture="two"), frames=SETTLE)
    assert rig.snap.state == "scrolling"
    rig.show(hand(0.5, 0.5), frames=9)
    rig.show(hand(0.5, 0.5, gesture="fist"), frames=SETTLE)
    assert rig.snap.state == "holding"
    empty = Rig(FakeDesktop(windows=[]))
    empty.start()
    empty.grab_at(0.5, 0.5)
    assert empty.snap.state == "holding nothing"


def test_settings_come_from_the_config_with_down_below_up(mock_config):
    mock_config.gesture_snap_finger_up = 1.0
    mock_config.gesture_snap_finger_down = 1.2
    mock_config.gesture_snap_debounce_ms = 150
    loaded = WindowSnapSettings.from_config(mock_config)
    assert loaded.finger_down < loaded.finger_up
    assert loaded.debounce == pytest.approx(0.15)


def test_a_gesture_change_never_makes_the_pointer_hop_and_it_catches_up_with_the_palm():
    rig = Rig(FakeDesktop())
    rig.start()
    aim = rig.desk.at
    rig.show(hand(0.52, 0.52, gesture="two"), frames=SETTLE)  # the palm shifts while the hand changes
    rig.show(hand(0.52, 0.52), frames=SETTLE)
    first = rig.desk.at
    assert abs(first[0] - aim[0]) < 40  # no hop of the full shift (0.02 of the camera = 77 pixels here)
    rig.show(hand(0.52, 0.52), frames=40)
    assert rig.desk.at[0] == pytest.approx(960 + 0.02 * 640 * 6, abs=3)  # back on the palm's place
