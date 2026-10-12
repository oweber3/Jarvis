"""The air mouse gesture model on synthetic hands and a fake pointer: moving, clicking, dragging, clutch and
always letting go of the button."""
import random
import statistics

import pytest

from jarvis.gestures.air_mouse import AirMouse, AirMouseSettings
from tests.synthetic_hands import FakePointer, frame, hand

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
    values = dict(hand="any", comfort_box=0.5, pinch_start=0.25, pinch_release=0.40, debounce=0.05, hold=0.35,
                  min_cutoff=1e6, beta=0.0)
    values.update(overrides)
    return AirMouseSettings(**values)


class Rig:
    """An air mouse fed one frame every 1/30 s, with the watchdog clock following the frames."""

    def __init__(self, pointer=None, **overrides):
        self.pointer = pointer or FakePointer()
        self.clock = Clock()
        self.mouse = AirMouse(self.pointer, settings(**overrides), clock=self.clock)
        self.t = 0.0

    def show(self, *hands, frames=1):
        for _ in range(frames):
            self.t += 1 / FPS
            self.clock.now = self.t
            self.mouse.on_frame(frame(self.t, *hands))
            self.mouse.tick()

    def wait(self, seconds):
        """Time passes with no frames at all (a frozen or unplugged camera)."""
        end = self.t + seconds
        while self.clock.now < end:
            self.clock.now = min(end, self.clock.now + 0.05)
            self.mouse.tick()
        self.t = end

    def glide(self, start, end, *, pinch=OPEN, frames=10):
        """Move the fingertip in a straight line from ``start`` to ``end`` over ``frames`` frames."""
        for i in range(1, frames + 1):
            f = i / frames
            self.show(hand(start[0] + (end[0] - start[0]) * f, start[1] + (end[1] - start[1]) * f, pinch=pinch))

    def pinch_at(self, x, y, *, frames):
        """Close the fingers at (x, y) (the tip drifts as real fingers do), hold for ``frames`` frames."""
        self.show(hand(x, y, pinch=0.35))
        self.show(hand(x + 0.01, y + 0.01, pinch=PINCHED), frames=frames)

    def let_go(self, x, y):
        self.show(hand(x, y, pinch=0.35))
        self.show(hand(x - 0.02, y + 0.02, pinch=OPEN), frames=3)


# --- moving ----------------------------------------------------------------------------------------

def test_the_pointer_starts_where_it_is_and_follows_the_fingertip():
    rig = Rig(FakePointer(at=(500, 400)))
    rig.show(hand(0.3, 0.3))
    assert rig.pointer.at == (500, 400)  # no jump when the hand appears
    rig.glide((0.3, 0.3), (0.4, 0.4))
    x, y = rig.pointer.at
    assert x > 500 and y > 400


def test_crossing_the_comfort_box_crosses_the_whole_desktop_of_several_monitors():
    monitors = [(-1920, 0, 0, 1080), (0, 0, 2560, 1440), (2560, 0, 4480, 1080)]
    left, right = -1920, 4480
    rig = Rig(FakePointer(monitors, at=(left, 500)), comfort_box=0.5)
    rig.show(hand(0.2, 0.5))
    rig.glide((0.2, 0.5), (0.7, 0.5), frames=20)  # half the camera's width
    assert rig.pointer.at[0] == pytest.approx(right - 1, abs=2)


def test_a_smaller_comfort_box_needs_less_hand_movement():
    def travel(box):
        rig = Rig(FakePointer(at=(100, 500)), comfort_box=box)
        rig.show(hand(0.4, 0.5))
        rig.glide((0.4, 0.5), (0.45, 0.5))
        return rig.pointer.at[0] - 100
    assert travel(0.25) == pytest.approx(2 * travel(0.5), rel=0.02)


def test_up_and_down_use_the_same_scale_as_left_and_right():
    rig = Rig(FakePointer([(0, 0, 3840, 2160)], at=(1000, 1000)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.5 + 0.1, 0.5))
    across = rig.pointer.at[0] - 1000
    rig.glide((0.6, 0.5), (0.6, 0.5 + 0.1 * 640 / 480))  # the same distance in camera pixels, downwards
    down = rig.pointer.at[1] - 1000
    assert down == pytest.approx(across, abs=2)


def test_the_pointer_never_leaves_the_desktop():
    rig = Rig(FakePointer([(0, 0, 1920, 1080)], at=(100, 100)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.0, 0.0))
    assert rig.pointer.at == (0, 0)
    rig.glide((0.0, 0.0), (1.0, 1.0), frames=20)
    assert rig.pointer.at == (1919, 1079)


def test_a_gap_between_monitors_of_different_heights_is_never_a_pointer_position():
    monitors = [(0, 0, 1920, 1080), (1920, 0, 3840, 600)]  # the right monitor is shorter
    rig = Rig(FakePointer(monitors, at=(1800, 1000)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.55, 0.5))  # rightwards along the bottom of the tall monitor
    x, y = rig.pointer.at
    assert any(l <= x < r and t <= y < b for l, t, r, b in monitors)


def test_moving_back_from_an_edge_moves_the_pointer_at_once():
    rig = Rig(FakePointer(at=(50, 500)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.2, 0.5))  # far past the left edge
    assert rig.pointer.at[0] == 0
    rig.glide((0.2, 0.5), (0.22, 0.5), frames=2)
    assert rig.pointer.at[0] > 0


# --- clutch ----------------------------------------------------------------------------------------

def test_an_open_palm_holds_the_pointer_still_and_resuming_does_not_jump():
    rig = Rig(FakePointer(at=(900, 500)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.55, 0.5))
    held = rig.pointer.at
    for i in range(10):
        rig.show(hand(0.55 - i * 0.03, 0.5, palm=True))  # reposition the hand with the palm open
    assert rig.pointer.at == held
    rig.show(hand(0.25, 0.5))
    assert rig.pointer.at == held
    rig.glide((0.25, 0.5), (0.3, 0.5))
    assert rig.pointer.at[0] > held[0]


def test_a_hand_out_of_view_holds_the_pointer_and_its_return_does_not_jump():
    rig = Rig(FakePointer(at=(900, 500)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.55, 0.5))
    held = rig.pointer.at
    rig.show(frames=15)  # no hands for half a second
    rig.show(hand(0.2, 0.8))
    assert rig.pointer.at == held


def test_moving_the_real_mouse_while_clutched_is_respected():
    rig = Rig(FakePointer(at=(900, 500)))
    rig.show(hand(0.5, 0.5))
    rig.show(frames=15)
    rig.pointer.at = (300, 300)  # the user moved the physical mouse
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.52, 0.5), frames=2)
    assert rig.pointer.at[0] > 300 and rig.pointer.at[1] == 300


# --- clicking --------------------------------------------------------------------------------------

def test_a_quick_pinch_clicks_once_where_the_pointer_was_before_the_fingers_closed():
    rig = Rig(FakePointer(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    rig.glide((0.5, 0.5), (0.52, 0.5), frames=2)
    target = rig.pointer.at
    rig.pinch_at(0.52, 0.5, frames=4)
    rig.let_go(0.52, 0.5)
    assert rig.pointer.buttons() == [("press", *target), ("release", *target)]
    assert not rig.pointer.down


def test_fingers_that_close_without_a_full_pinch_never_click():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, pinch=0.3), frames=10)  # between the two thresholds
    rig.show(hand(0.5, 0.5, pinch=OPEN), frames=3)
    assert rig.pointer.buttons() == []


def test_a_pinch_shorter_than_the_debounce_is_ignored():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, pinch=PINCHED))  # a single frame, 33 ms
    rig.show(hand(0.5, 0.5, pinch=OPEN), frames=5)
    assert rig.pointer.buttons() == []


def test_a_ratio_hovering_between_the_thresholds_keeps_the_pinch_and_clicks_once():
    rig = Rig(hold=2.0)
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, pinch=PINCHED), frames=3)
    for ratio in (0.3, 0.38, 0.27, 0.39, 0.3):
        rig.show(hand(0.5, 0.5, pinch=ratio))
    assert rig.pointer.buttons() == []
    rig.show(hand(0.5, 0.5, pinch=OPEN), frames=3)
    assert [e[0] for e in rig.pointer.buttons()] == ["press", "release"]


def test_the_pointer_stays_still_while_the_fingers_close_and_open():
    rig = Rig(FakePointer(at=(700, 400)), hold=5.0)
    rig.show(hand(0.5, 0.5))
    before = rig.pointer.at
    rig.show(hand(0.53, 0.47, pinch=0.35), frames=3)
    rig.show(hand(0.55, 0.45, pinch=0.3), frames=3)
    rig.show(hand(0.53, 0.47, pinch=0.35), frames=3)
    assert rig.pointer.at == before
    rig.show(hand(0.53, 0.47, pinch=OPEN))
    rig.glide((0.53, 0.47), (0.56, 0.47), frames=3)
    assert rig.pointer.at[0] > before[0]


# --- dragging --------------------------------------------------------------------------------------

def test_a_held_pinch_drags_from_the_anchor_and_drops_where_the_hand_ends():
    rig = Rig(FakePointer(at=(700, 400)))
    rig.show(hand(0.5, 0.5))
    anchor = rig.pointer.at
    rig.pinch_at(0.5, 0.5, frames=15)  # half a second
    assert rig.pointer.buttons() == [("press", *anchor)]
    rig.glide((0.51, 0.51), (0.61, 0.51), pinch=PINCHED)
    moved = rig.pointer.at
    assert moved[0] > anchor[0] + 300 and rig.pointer.down
    rig.let_go(0.61, 0.51)
    assert rig.pointer.buttons()[-1] == ("release", *moved)  # opening the fingers does not shift the drop
    assert not rig.pointer.down


def test_a_pinch_ratio_between_the_thresholds_keeps_dragging():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    rig.glide((0.51, 0.51), (0.55, 0.51), pinch=0.33)
    assert rig.pointer.down


# --- never leaving the button down -----------------------------------------------------------------

def test_losing_the_hand_during_a_drag_releases_the_button():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    assert rig.pointer.down
    rig.show(frames=10)  # frames keep coming, with no hand in them
    assert not rig.pointer.down


def test_a_camera_that_stops_delivering_frames_during_a_drag_releases_the_button():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    rig.wait(1.0)
    assert not rig.pointer.down


def test_a_momentary_tracking_dropout_does_not_drop_what_is_being_dragged():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    rig.show(frames=2)  # 66 ms without the hand
    rig.show(hand(0.51, 0.51, pinch=PINCHED))
    assert rig.pointer.down and [e[0] for e in rig.pointer.buttons()] == ["press"]


def test_a_pinch_interrupted_by_losing_the_hand_does_not_click():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, pinch=PINCHED), frames=3)
    rig.show(frames=15)
    rig.show(hand(0.5, 0.5, pinch=OPEN), frames=3)
    assert rig.pointer.buttons() == []


def test_closing_releases_a_held_button():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    rig.mouse.close()
    assert not rig.pointer.down


def test_closing_without_a_held_button_sends_no_button_event():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.mouse.close()
    assert rig.pointer.buttons() == []


def test_frames_after_closing_do_nothing():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.mouse.close()
    rig.glide((0.5, 0.5), (0.6, 0.5))
    rig.pinch_at(0.6, 0.5, frames=15)
    assert rig.pointer.events == []


def test_a_pointer_that_refuses_input_does_not_break_the_air_mouse():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pointer.fail = True
    rig.glide((0.5, 0.5), (0.6, 0.5))
    rig.pointer.fail = False
    rig.glide((0.6, 0.5), (0.65, 0.5))
    assert rig.pointer.moves()


# --- which hand ------------------------------------------------------------------------------------

def test_only_the_chosen_hand_moves_the_pointer():
    rig = Rig(FakePointer(at=(900, 500)), hand="right")
    for i in range(10):
        rig.show(hand(0.3 + i * 0.01, 0.5, side="left"))
    assert rig.pointer.moves() == []


def test_with_any_hand_the_hand_in_use_keeps_control_when_another_appears():
    rig = Rig(FakePointer(at=(900, 500)))
    rig.show(hand(0.5, 0.5, side="left", score=0.7))
    for i in range(1, 6):
        rig.show(hand(0.5 + i * 0.01, 0.5, side="left", score=0.7),
                 hand(0.8, 0.2 + i * 0.05, side="right", score=0.99))
    x, y = rig.pointer.at
    assert x > 900 and y == 500


# --- smoothing -------------------------------------------------------------------------------------

def test_smoothing_steadies_a_jittery_fingertip():
    def spread(**overrides):
        rng = random.Random(7)
        rig = Rig(FakePointer(at=(900, 500)), **overrides)
        xs = []
        for _ in range(150):
            rig.show(hand(0.5 + rng.uniform(-0.003, 0.003), 0.5 + rng.uniform(-0.003, 0.003)))
            xs.append(rig.pointer.at[0])
        return statistics.pstdev(xs[30:])
    assert spread(min_cutoff=1.0, beta=0.005) < spread() / 2


def test_smoothing_still_follows_a_deliberate_movement():
    rig = Rig(FakePointer(at=(200, 500)), min_cutoff=1.0, beta=0.005)
    rig.show(hand(0.4, 0.5))
    rig.glide((0.4, 0.5), (0.5, 0.5), frames=6)
    rig.show(hand(0.5, 0.5), frames=30)
    assert rig.pointer.at[0] == pytest.approx(200 + 0.1 * 640 * 1920 / 320, abs=10)


# --- settings --------------------------------------------------------------------------------------

def test_settings_come_from_the_config_with_a_release_above_the_start(mock_config):
    mock_config.air_mouse_pinch_start = 0.3
    mock_config.air_mouse_pinch_release = 0.2
    mock_config.air_mouse_hold_ms = 500
    loaded = AirMouseSettings.from_config(mock_config)
    assert loaded.pinch_release > loaded.pinch_start
    assert loaded.hold == pytest.approx(0.5)


def test_the_state_names_what_the_hand_is_doing():
    rig = Rig()
    assert rig.mouse.state == "paused"
    rig.show(hand(0.5, 0.5))
    assert rig.mouse.state == "moving"
    rig.show(hand(0.5, 0.5, pinch=0.35))
    assert rig.mouse.state == "pinch closing"
    rig.show(hand(0.5, 0.5, pinch=PINCHED), frames=3)
    assert rig.mouse.state == "pinched"
    rig.show(hand(0.5, 0.5, pinch=PINCHED), frames=12)
    assert rig.mouse.state == "dragging"


# --- review regressions ----------------------------------------------------------------------------

def test_another_hand_never_takes_over_during_a_dropout():
    rig = Rig(FakePointer(at=(900, 500)))
    rig.show(hand(0.5, 0.5, side="right"))
    rig.pinch_at(0.5, 0.5, frames=15)
    assert rig.pointer.down
    rig.show(hand(0.2, 0.2, palm=True, side="left"))  # the right hand missed for one frame
    rig.show(hand(0.51, 0.51, pinch=PINCHED, side="right"), hand(0.2, 0.2, palm=True, side="left"), frames=5)
    assert rig.pointer.down
    assert [e[0] for e in rig.pointer.buttons()] == ["press"]


def test_two_hands_with_the_same_label_do_not_make_the_pointer_jump():
    rig = Rig(FakePointer(at=(900, 500)))
    a, b = (0.3, 0.5), (0.7, 0.5)
    rig.show(hand(*a))
    for i in range(6):
        pair = (hand(*a), hand(*b)) if i % 2 else (hand(*b), hand(*a))
        rig.show(*pair)
    assert rig.pointer.moves() == []


def test_a_release_that_fails_at_first_is_retried_when_stopping():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    failures = {"left": 2}
    real = rig.pointer.release

    def flaky():
        if failures["left"]:
            failures["left"] -= 1
            raise OSError("locked")
        real()
    rig.pointer.release = flaky
    rig.mouse.close()
    assert not rig.pointer.down


def test_the_watchdog_retries_a_failed_release():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    real = rig.pointer.release
    rig.pointer.release = lambda: (_ for _ in ()).throw(OSError("locked"))
    rig.show(frames=10)  # hand lost: the release fails
    assert rig.pointer.down
    rig.pointer.release = real
    rig.wait(0.2)
    assert not rig.pointer.down


def test_a_drop_after_a_refused_press_sends_no_button_up():
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.show(hand(0.5, 0.5, pinch=0.35))
    real = rig.pointer.press
    rig.pointer.press = lambda: (_ for _ in ()).throw(OSError("refused"))
    rig.show(hand(0.5, 0.5, pinch=PINCHED), frames=15)
    rig.pointer.press = real
    rig.let_go(0.5, 0.5)
    assert rig.pointer.buttons() == []


def test_slow_detection_does_not_drop_a_drag():
    """Frames arrive late (captured long before delivery) but steadily: the hand is not lost."""
    rig = Rig()
    rig.show(hand(0.5, 0.5))
    rig.pinch_at(0.5, 0.5, frames=15)
    for i in range(10):
        rig.t += 1 / FPS
        rig.clock.now = rig.t + 0.4  # delivered 400 ms after capture
        rig.mouse.on_frame(frame(rig.t, hand(0.51, 0.51, pinch=PINCHED)))
        rig.mouse.tick()
    assert rig.pointer.down


def test_a_hold_shorter_than_the_debounce_still_allows_a_click(mock_config):
    mock_config.air_mouse_debounce_ms = 300
    mock_config.air_mouse_hold_ms = 100
    loaded = AirMouseSettings.from_config(mock_config)
    assert loaded.hold > loaded.debounce


def test_a_side_on_pinch_clicks_even_when_the_trackers_depth_is_off():
    """Measured on a real webcam: side-on, the tips touch in the image while the 3D estimate keeps them apart."""
    rig = Rig()
    rig.show(hand(0.5, 0.5, pinch=0.6, depth_error=0.03))
    rig.show(hand(0.5, 0.5, pinch=0.15, depth_error=0.03), frames=4)
    rig.show(hand(0.5, 0.5, pinch=0.6, depth_error=0.03), frames=3)
    assert [e[0] for e in rig.pointer.buttons()] == ["press", "release"]
