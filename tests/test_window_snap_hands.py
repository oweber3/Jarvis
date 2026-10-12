"""Gesture snapping beyond one hand's shapes: a raised open hand starts listening, a spoken click lands at the
pointer, two open hands zoom, and crossed wrists turn the mode off. Synthetic hands, fake desktop."""
import pytest

from tests.synthetic_hands import FakeDesktop, FakeWindow, hand
from tests.test_window_snap import FPS, SETTLE, Rig


class EventRig(Rig):
    """A rig that records what the gesture model asks its owner to do (talk, switch off)."""

    def __init__(self, desk=None, **overrides):
        super().__init__(desk, **overrides)
        from jarvis.gestures.window_snap import WindowSnap
        from tests.test_window_snap import settings
        self.asked = []
        self.snap = WindowSnap(self.desk, settings(**overrides), clock=self.clock, on_event=self.asked.append)

    def hold(self, seconds, *hands):
        self.show(*hands, frames=int(round(seconds * FPS)))


def other(x=0.25, y=0.5, gesture="one"):
    """A second hand, well away from the driving one."""
    return hand(x, y, gesture=gesture, side="left", score=0.6)


# --- a raised open hand starts listening -----------------------------------------------------------

def test_an_open_hand_held_up_for_a_second_asks_to_listen_once():
    rig = EventRig()
    rig.hold(0.8, hand(gesture="open"))
    assert rig.asked == []
    rig.hold(0.4, hand(gesture="open"))
    assert rig.asked == ["talk"]
    rig.hold(2.0, hand(gesture="open"))
    assert rig.asked == ["talk"]  # once per raise


def test_after_aiming_an_open_hand_held_up_also_asks_to_listen():
    rig = EventRig()
    rig.start()
    rig.hold(1.2, hand(gesture="open"))
    assert rig.asked == ["talk"]


def test_changing_gesture_or_lowering_the_hand_lets_it_ask_again():
    rig = EventRig()
    rig.hold(1.2, hand(gesture="open"))
    rig.show(hand(), frames=SETTLE)
    rig.hold(1.2, hand(gesture="open"))
    rig.wait(0.5)  # out of view
    rig.hold(1.2, hand(gesture="open"))
    assert rig.asked == ["talk", "talk", "talk"]


def test_opening_the_hand_to_snap_a_window_never_asks_to_listen():
    rig = EventRig(FakeDesktop(windows=[FakeWindow((0, 0, 1920, 1080))]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    rig.hold(2.0, hand(gesture="open"))
    assert rig.desk.of("drop") and rig.asked == []


def test_an_open_hand_beside_a_second_hand_does_not_ask_to_listen():
    rig = EventRig()
    rig.hold(1.5, hand(0.6, 0.5, gesture="open"), other())
    assert rig.asked == []


def test_with_the_setting_off_a_raised_hand_only_pauses():
    rig = EventRig(talk=False)
    rig.hold(2.0, hand(gesture="open"))
    assert rig.asked == []


# --- a spoken click lands at the pointer -----------------------------------------------------------

@pytest.mark.parametrize("kind", ["click", "double_click", "right_click"])
def test_a_click_lands_where_the_hand_put_the_pointer(kind):
    rig = EventRig()
    rig.start()
    rig.glide((0.5, 0.5), (0.6, 0.45))
    assert rig.snap.click(kind) == "clicked"
    assert rig.desk.of("click") == [("click", kind, rig.desk.at)]


def test_a_click_with_the_hand_paused_or_out_of_view_clicks_where_the_pointer_stands():
    rig = EventRig()
    rig.start()
    rig.hold(0.3, hand(gesture="open"))
    assert rig.snap.click("click") == "clicked"
    rig.wait(1.0)
    assert rig.snap.click("click") == "clicked"
    assert len(rig.desk.of("click")) == 2


def test_a_click_while_holding_a_window_or_scrolling_is_refused():
    rig = EventRig(FakeDesktop(windows=[FakeWindow((0, 0, 1920, 1080))]))
    rig.start()
    rig.grab_at(0.5, 0.5)
    assert rig.snap.click("click") == "busy"
    rig.raise_finger(0.5, 0.5)
    rig.two_at(0.5, 0.5)
    rig.show(hand(0.51, 0.3, gesture="two"), frames=10)
    assert rig.snap.state == "scrolling"
    assert rig.snap.click("click") == "busy"
    assert rig.desk.of("click") == []


def test_a_click_windows_refuses_raises_and_tracking_continues():
    rig = EventRig()
    rig.start()
    rig.desk.fail = True
    with pytest.raises(OSError):
        rig.snap.click("click")
    rig.desk.fail = False
    rig.glide((0.5, 0.5), (0.6, 0.5))
    assert rig.desk.of("pointer")


# --- two open hands zoom ---------------------------------------------------------------------------

def zoom_rig(**overrides):
    rig = EventRig(**overrides)
    rig.start(0.6, 0.5)
    rig.show(hand(0.6, 0.5, gesture="open", score=0.95), other(0.4, 0.5, "open"), frames=SETTLE + 2)
    return rig


def spread(rig, start, end, frames=20, driving="open", second="open"):
    """Move the hands symmetrically about x = 0.5 from ``start`` to ``end`` apart (in image widths)."""
    for i in range(1, frames + 1):
        half = (start + (end - start) * i / frames) / 2
        rig.show(hand(0.5 + half, 0.5, gesture=driving, score=0.95), other(0.5 - half, 0.5, second))


def test_pulling_two_open_hands_apart_zooms_in_and_pushing_them_together_zooms_out():
    rig = zoom_rig()
    assert rig.snap.state == "zooming"
    spread(rig, 0.2, 0.4)
    zoomed_in = rig.desk.zoomed()
    assert zoomed_in >= 4 * 120  # twice as far apart: about five 15% steps
    spread(rig, 0.4, 0.2)
    assert rig.desk.zoomed() == pytest.approx(0, abs=120)
    spread(rig, 0.2, 0.1)
    assert rig.desk.zoomed() <= -3 * 120


def test_zoom_notches_are_whole_wheel_notches_at_the_still_pointer():
    rig = zoom_rig()
    pointer = rig.desk.at
    pointer_moves = len(rig.desk.of("pointer"))
    spread(rig, 0.2, 0.35)
    assert all(abs(e[1]) == 120 for e in rig.desk.of("zoom"))
    assert len(rig.desk.of("pointer")) == pointer_moves and rig.desk.at == pointer


def test_steady_hands_near_a_step_boundary_do_not_flicker_the_zoom():
    rig = zoom_rig()
    import math
    boundary = 0.2 * math.sqrt(1.15)  # half a step past the start
    for i in range(60):
        spread(rig, boundary, boundary + (0.004 if i % 2 else -0.004), frames=1)
    assert len(rig.desk.of("zoom")) <= 1


def test_a_hand_misread_as_a_fist_while_zooming_grabs_nothing_and_the_zoom_goes_on():
    rig = zoom_rig()
    rig.desk.windows.append(FakeWindow((0, 0, 1920, 1080)))
    spread(rig, 0.2, 0.25, frames=6, driving="fist")
    assert rig.snap.state == "zooming"
    spread(rig, 0.25, 0.4)
    assert [e for e in rig.desk.events if e[0] == "grab"] == []
    assert rig.desk.zoomed() > 0


def test_the_zoom_ends_when_the_hands_stop_being_open_and_aiming_resumes():
    rig = zoom_rig()
    rig.hold(0.5, hand(0.6, 0.5, gesture="one", score=0.95))
    assert rig.snap.state == "aiming"
    before = len(rig.desk.of("pointer"))
    rig.glide((0.6, 0.5), (0.7, 0.4))
    assert len(rig.desk.of("pointer")) > before


def test_a_zoom_uses_up_the_raised_hand_talk():
    rig = zoom_rig()
    rig.hold(2.0, hand(0.6, 0.5, gesture="open", score=0.95))
    assert rig.asked == []


def test_one_open_hand_and_a_pointing_hand_do_not_zoom():
    rig = EventRig()
    rig.show(hand(0.6, 0.5, gesture="open", score=0.95), other(0.4, 0.5, "one"), frames=SETTLE)
    spread(rig, 0.2, 0.4, second="one")
    assert rig.desk.of("zoom") == []


def test_a_click_while_zooming_is_refused():
    rig = zoom_rig()
    assert rig.snap.click("click") == "busy"


def test_with_two_hands_off_there_is_no_zoom():
    rig = zoom_rig(two_hands=False)
    spread(rig, 0.2, 0.4)
    assert rig.desk.of("zoom") == []


# --- crossed wrists turn the mode off --------------------------------------------------------------

def crossed(gap=0.02, lean=35.0, y=0.65, gesture="open"):
    """Two hands with the wrists ``gap`` image widths apart, the left one leaning left and the right one right."""
    return (hand(gesture=gesture, wrist=(0.5 + gap / 2, y), tilt=lean, side="left", score=0.95),
            hand(gesture=gesture, wrist=(0.5 - gap / 2, y), tilt=-lean, side="right", score=0.9))


def test_crossed_wrists_held_turn_the_mode_off_once():
    rig = EventRig()
    rig.hold(0.4, *crossed())
    assert rig.asked == []
    rig.hold(0.4, *crossed())
    assert rig.asked == ["switch off"]
    rig.hold(1.0, *crossed())
    assert rig.asked == ["switch off"]


@pytest.mark.parametrize("gesture", ["fist", "one"])
def test_crossed_wrists_count_whatever_the_fingers_do(gesture):
    rig = EventRig()
    rig.hold(0.8, *crossed(gesture=gesture))
    assert rig.asked == ["switch off"]


def test_crossed_wrists_for_a_moment_do_nothing():
    rig = EventRig()
    rig.hold(0.3, *crossed())
    rig.hold(1.0, hand(0.7, 0.5))
    assert "switch off" not in rig.asked


def test_hands_side_by_side_pointing_up_do_not_turn_it_off():
    rig = EventRig()
    rig.hold(1.5, *crossed(gap=0.02, lean=0.0))
    rig.hold(1.5, *crossed(gap=0.1, lean=5.0))
    assert "switch off" not in rig.asked


def test_hands_fanned_out_with_the_wrists_apart_do_not_turn_it_off():
    rig = EventRig()
    rig.hold(1.5, *crossed(gap=0.3, lean=35.0))
    assert "switch off" not in rig.asked


def test_with_two_hands_off_crossed_wrists_do_nothing():
    rig = EventRig(two_hands=False)
    rig.hold(1.5, *crossed())
    assert "switch off" not in rig.asked


# --- review: two hands never grab, zoom never bursts, crossing tolerates dropouts -----------------

def test_crossing_the_wrists_with_fists_never_grabs_a_window():
    rig = EventRig(FakeDesktop(windows=[FakeWindow((0, 0, 1920, 1080))]))
    rig.hold(0.4, *crossed(gap=0.12, lean=5.0, gesture="fist"))  # arms coming together
    rig.hold(0.8, *crossed(gesture="fist"))
    assert rig.asked == ["switch off"]
    assert [e for e in rig.desk.events if e[0] in ("grab", "window")] == []


def test_a_fist_with_a_second_hand_in_view_grabs_nothing():
    rig = EventRig(FakeDesktop(windows=[FakeWindow((0, 0, 1920, 1080))]))
    rig.start(0.6, 0.5)
    rig.show(hand(0.605, 0.505, gesture="fist", score=0.95), other(0.25, 0.5), frames=SETTLE + 3)
    assert rig.desk.of("grab") == []


def test_ending_a_zoom_with_fists_grabs_nothing_until_a_finger_is_raised():
    rig = zoom_rig()
    rig.desk.windows.append(FakeWindow((0, 0, 1920, 1080)))
    rig.hold(1.0, hand(0.6, 0.5, gesture="fist", score=0.95))
    assert rig.desk.of("grab") == []
    rig.show(hand(0.6, 0.5, score=0.95), frames=SETTLE)
    rig.show(hand(0.605, 0.505, gesture="fist", score=0.95), frames=SETTLE)
    assert rig.desk.of("grab")


def test_a_sudden_jump_in_the_palm_gap_zooms_one_notch_a_frame_at_most():
    rig = zoom_rig()
    rig.show(hand(0.8, 0.5, gesture="open", score=0.95), other(0.2, 0.5, "open"))  # 0.2 to 0.6 apart at once
    assert len(rig.desk.of("zoom")) == 1


def test_a_second_hand_coming_back_during_a_zoom_does_not_burst():
    rig = zoom_rig()
    rig.show(hand(0.6, 0.5, gesture="open", score=0.95), frames=4)  # the other hand briefly lost
    rig.show(hand(0.6, 0.5, gesture="open", score=0.95), other(0.1, 0.5, "open"), frames=10)
    assert rig.desk.of("zoom") == []


def test_a_one_frame_tracking_dropout_does_not_break_the_crossed_wrists_hold():
    rig = EventRig()
    for _ in range(4):
        rig.hold(0.17, *crossed())
        rig.show(crossed()[0])  # one frame with only one hand found
    assert rig.asked == ["switch off"]


def test_with_two_hand_gestures_off_a_second_hand_does_not_block_talking():
    rig = EventRig(two_hands=False)
    rig.hold(1.5, hand(0.6, 0.5, gesture="open", score=0.95), other())
    assert rig.asked == ["talk"]
