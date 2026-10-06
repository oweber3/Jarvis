"""Virtual desktop control through an injected backend (the real one is read-only checked)."""
import pytest

from jarvis.platform.windows import virtual_desktops as vd


class FakeDesktops:
    def __init__(self, count=3, current=2):
        self.count, self.current_number = count, current
        self.windows = {}
        self.log = []

    def desktop_count(self):
        return self.count

    def current(self):
        return self.current_number

    def go(self, number):
        self.log.append(('go', number))
        self.current_number = number

    def create(self):
        self.log.append(('create',))
        self.count += 1
        return self.count

    def remove(self, number):
        self.log.append(('remove', number))
        self.count -= 1
        if self.current_number >= number and self.current_number > 1:
            self.current_number -= 1

    def move(self, hwnd, number):
        self.log.append(('move', hwnd, number))
        self.windows[hwnd] = number


@pytest.fixture
def desktops(monkeypatch):
    fake = FakeDesktops()
    monkeypatch.setattr(vd, '_backend', lambda: fake)
    return fake


def test_state_reports_the_count_and_the_current_desktop(desktops):
    assert vd.state() == vd.DesktopState(count=3, current=2)


@pytest.mark.parametrize('target,expected', [('1', 1), ('3', 3), (' 1 ', 1), ('next', 3), ('previous', 1)])
def test_switch_accepts_a_number_next_or_previous(desktops, target, expected):
    assert vd.switch(target).current == expected
    assert desktops.log == [('go', expected)]


@pytest.mark.parametrize('target', ['0', '4', '-1', 'two', '', 'first', '2.5'])
def test_switch_rejects_a_desktop_that_does_not_exist_without_moving(desktops, target):
    with pytest.raises(ValueError) as error:
        vd.switch(target)
    assert desktops.log == []
    assert '3' in str(error.value)


def test_switching_past_either_end_is_an_error_not_a_wrap(desktops):
    desktops.current_number = 3
    with pytest.raises(ValueError):
        vd.switch('next')
    desktops.current_number = 1
    with pytest.raises(ValueError):
        vd.switch('previous')
    assert desktops.log == []


def test_switching_to_the_current_desktop_changes_nothing(desktops):
    assert vd.switch('2').current == 2
    assert desktops.log == []


def test_new_desktop_is_created_and_switched_to(desktops):
    state = vd.create()
    assert state == vd.DesktopState(count=4, current=4)
    assert desktops.log == [('create',), ('go', 4)]


def test_close_removes_the_named_desktop_and_reports_the_new_state(desktops):
    state = vd.close('3')
    assert desktops.log == [('remove', 3)]
    assert state.count == 2


def test_close_without_a_target_closes_the_current_desktop(desktops):
    vd.close('')
    assert desktops.log == [('remove', 2)]


def test_the_last_desktop_cannot_be_closed(monkeypatch):
    fake = FakeDesktops(count=1, current=1)
    monkeypatch.setattr(vd, '_backend', lambda: fake)
    with pytest.raises(ValueError):
        vd.close('1')
    assert fake.log == []


def test_a_window_moves_to_a_numbered_desktop(desktops):
    result = vd.move_window(4242, '3')
    assert desktops.log == [('move', 4242, 3)]
    assert result == {'hwnd': 4242, 'desktop': 3}


def test_a_window_cannot_move_to_a_desktop_that_does_not_exist(desktops):
    with pytest.raises(ValueError):
        vd.move_window(4242, '9')
    assert desktops.log == []


def test_a_window_can_move_to_the_next_or_previous_desktop(desktops):
    assert vd.move_window(7, 'next')['desktop'] == 3
    assert vd.move_window(7, 'previous')['desktop'] == 1


@pytest.mark.integration
def test_the_real_virtual_desktop_count_can_be_read_without_changing_anything():
    state = vd.state()
    assert state.count >= 1 and 1 <= state.current <= state.count
