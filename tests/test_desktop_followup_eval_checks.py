"""The follow-up eval's checks accept the right desktop outcome and reject the wrong ones.

A check that cannot fail proves nothing, so each is exercised with a correct outcome and with the
specific mistakes it exists to catch (wrong window, relaunch, wrong display).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import desktop_followups as fu  # noqa: E402
from desktop_sim import PRIMARY, SECOND, SimulatedDesktop  # noqa: E402


def scenario(name):
    return next(s for s in fu.SCENARIOS if s.name == name)


def begin(desktop, state):
    fu._snapshot_baseline(desktop, state)
    fu._remember_existing(desktop, state)


def check(turn, desktop, state):
    return turn.check(desktop, state, fu.TurnRun(turn.text))


@pytest.fixture
def word_opened():
    """A desktop on which "open Word" just launched one new Word window, beside an older one."""
    desktop = SimulatedDesktop()
    older = desktop.spawn("WINWORD", PRIMARY)
    state = {}
    begin(desktop, state)
    desktop.launches.append("Word")
    new = desktop.spawn("WINWORD", PRIMARY)
    opened = scenario("word_with_existing_window").turns[0]
    assert check(opened, desktop, state) is None
    assert state["word"] == new.hwnd
    begin(desktop, state)
    return desktop, state, new, older


@pytest.mark.unit
class TestFollowUpChecks:
    def test_open_rejects_no_or_several_new_windows(self):
        desktop, state = SimulatedDesktop(), {}
        begin(desktop, state)
        opened = scenario("word_move_fullscreen_beside").turns[0]
        assert "found 0" in check(opened, desktop, state)
        desktop.spawn("WINWORD")
        desktop.spawn("WINWORD")
        assert "found 2" in check(opened, desktop, state)

    def test_move_accepts_only_the_window_jarvis_opened(self, word_opened):
        desktop, state, new, older = word_opened
        move = scenario("word_with_existing_window").turns[1]
        older.monitor = SECOND
        assert check(move, desktop, state) is not None, "moving the older window is not the follow-up"
        older.monitor = PRIMARY
        new.monitor = SECOND
        assert check(move, desktop, state) is None

    def test_move_rejects_a_relaunch(self, word_opened):
        desktop, state, new, _ = word_opened
        new.monitor = SECOND
        desktop.launches.append("Word")
        assert "launched again" in check(scenario("word_with_existing_window").turns[1], desktop, state)

    def test_maximise_requires_the_same_window_on_the_second_display(self, word_opened):
        desktop, state, new, _ = word_opened
        full_screen = scenario("word_move_fullscreen_beside").turns[2]
        new.monitor, new.state = SECOND, "normal"
        assert "not maximised" in check(full_screen, desktop, state)
        new.state, new.monitor = "maximised", PRIMARY
        assert "off the second monitor" in check(full_screen, desktop, state)
        new.monitor = SECOND
        assert check(full_screen, desktop, state) is None

    def test_beside_requires_the_partner_on_the_same_display(self, word_opened):
        desktop, state, new, _ = word_opened
        beside = scenario("word_move_fullscreen_beside").turns[3]
        new.monitor = SECOND
        music = desktop.spawn("AppleMusic", PRIMARY)
        assert "not next to" in check(beside, desktop, state)
        music.monitor = SECOND
        assert check(beside, desktop, state) is None

    def test_close_rejects_closing_another_window(self):
        desktop, state = SimulatedDesktop(), {}
        notepad = desktop.spawn("notepad", SECOND)
        state["notepad"] = notepad.hwnd
        begin(desktop, state)
        close = scenario("notepad_move_maximise_close").turns[3]
        assert "still open" in check(close, desktop, state)
        bystander = desktop.open_windows("chrome")[0]
        bystander.open = notepad.open = False
        assert "did not mean" in check(close, desktop, state)
        bystander.open = True
        assert check(close, desktop, state) is None
