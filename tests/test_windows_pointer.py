"""The Windows pointer output: the primary button is pressed and released, swapped buttons are respected and
a refused event is an error. Default runs never send real input."""
import sys

import pytest

from jarvis.platform.windows import pointer


class Recorder:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, flags):
        if self.fail:
            raise OSError("refused")
        self.sent.append(flags)


def test_press_and_release_send_the_left_button():
    sender = Recorder()
    out = pointer.WindowsPointer(sender=sender, swapped=False)
    out.press()
    out.release()
    assert sender.sent == [pointer.MOUSEEVENTF_LEFTDOWN, pointer.MOUSEEVENTF_LEFTUP]


def test_swapped_buttons_still_give_the_primary_click():
    sender = Recorder()
    out = pointer.WindowsPointer(sender=sender, swapped=True)
    out.press()
    out.release()
    assert sender.sent == [pointer.MOUSEEVENTF_RIGHTDOWN, pointer.MOUSEEVENTF_RIGHTUP]


def test_a_refused_event_is_an_error():
    out = pointer.WindowsPointer(sender=Recorder(fail=True), swapped=False)
    with pytest.raises(OSError):
        out.press()


def test_the_input_record_has_the_size_windows_expects():
    expected = 40 if sys.maxsize > 2 ** 32 else 28
    assert pointer.input_record_size() == expected


@pytest.mark.interactive
@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_the_real_pointer_can_be_read_and_put_back_where_it_was():
    out = pointer.WindowsPointer()
    rectangles = out.desktop()
    x, y = out.position()
    assert any(l <= x < r and t <= y < b for l, t, r, b in rectangles)
    out.move_to(x, y)
    assert out.position() == (x, y)
