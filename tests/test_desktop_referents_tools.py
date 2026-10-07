"""Window actions leave a desktop referent behind, whichever route executed them.

The real ``appControl`` / ``windowControl`` tools run through the central ``run_tool_with_retries``
on a simulated desktop (only the OS layer is fake).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from desktop_sim import PRIMARY, SECOND, SimulatedDesktop  # noqa: E402

from jarvis.memory.desktop_referents import get_desktop_referents  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop only")


@pytest.fixture(autouse=True)
def fresh_referents():
    get_desktop_referents().clear()
    yield
    get_desktop_referents().clear()


@pytest.fixture
def desktop(mock_config):
    with SimulatedDesktop(mock_config) as sim:
        yield sim


def run(cfg, tool, **args):
    from jarvis.tools.registry import run_tool_with_retries
    return run_tool_with_retries(None, cfg, tool, args, "", "", "")


def referents():
    return get_desktop_referents().recent(max_age_sec=300)


@pytest.mark.unit
class TestRecordedOutcomes:
    def test_a_plain_launch_resolves_to_the_new_window_not_an_older_one(self, mock_config, desktop):
        older = desktop.spawn("WINWORD", PRIMARY)
        result = run(mock_config, "appControl", action="open", target="Word")
        assert result.success
        new = next(w for w in desktop.open_windows("WINWORD") if w.hwnd != older.hwnd)
        (first, *_rest) = referents()
        assert (first.application, first.process, first.hwnd) == ("Word", "WINWORD", new.hwnd)
        assert first.last_action == "open"

    def test_a_launch_whose_window_is_not_found_yet_stays_pending(self, mock_config, desktop, monkeypatch):
        monkeypatch.setattr(desktop, "spawn", lambda *a, **kw: None)  # the window has not appeared yet
        assert run(mock_config, "appControl", action="open", target="Word").success
        (first, *_rest) = referents()
        assert first.application == "Word" and first.hwnd is None

    def test_launch_and_place_records_where_the_window_went(self, mock_config, desktop):
        assert run(mock_config, "appControl", action="open", target="Word", monitor="second", zone="left").success
        word = desktop.only("WINWORD")
        (first, *_rest) = referents()
        assert (first.hwnd, first.monitor, first.zone, first.state) == (word.hwnd, SECOND, "left", "normal")

    def test_follow_up_actions_by_handle_update_the_same_window(self, mock_config, desktop):
        run(mock_config, "appControl", action="open", target="Word")
        word = desktop.only("WINWORD")
        assert run(mock_config, "windowControl", action="place", target=str(word.hwnd), monitor="2").success
        assert run(mock_config, "windowControl", action="maximise", target=str(word.hwnd)).success
        (first, *_rest) = referents()
        assert (first.application, first.hwnd, first.monitor, first.state, first.last_action) == (
            "Word", word.hwnd, SECOND, "maximised", "maximise")
        assert run(mock_config, "appControl", action="close", target=str(word.hwnd)).success
        assert referents()[0].state == "closing"

    def test_focusing_by_name_records_the_window_and_its_process(self, mock_config, desktop):
        music = desktop.spawn("AppleMusic", PRIMARY)
        assert run(mock_config, "appControl", action="focus", target="Apple Music").success
        (first, *_rest) = referents()
        assert (first.hwnd, first.process, first.last_action) == (music.hwnd, "AppleMusic", "focus")

    def test_listings_and_failures_record_nothing(self, mock_config, desktop):
        assert run(mock_config, "windowControl", action="list", target="").success
        assert run(mock_config, "windowControl", action="displays", target="").success
        assert not run(mock_config, "windowControl", action="place", target="chrome", monitor="9").success
        assert not run(mock_config, "appControl", action="focus", target="Zorbulon").success
        assert referents() == []

    def test_a_launch_whose_placement_failed_is_still_remembered(self, mock_config, desktop):
        desktop.fail_placement = "The application did not reach the requested position."
        result = run(mock_config, "appControl", action="open", target="Word", monitor="2")
        assert not result.success and json.loads(result.reply_text)["placement"] == "failed"
        (first, *_rest) = referents()
        assert (first.application, first.hwnd) == ("Word", desktop.only("WINWORD").hwnd)

    def test_no_title_or_path_reaches_the_record(self, mock_config, desktop):
        run(mock_config, "appControl", action="open", target="Word")
        run(mock_config, "appControl", action="focus", target="chrome")
        from jarvis.memory.desktop_referents import format_for_model, records_for_request
        shown = format_for_model(referents()) + json.dumps(records_for_request(referents()))
        assert "Document1" not in shown and "New Tab" not in shown
        assert "Program Files" not in shown and ".lnk" not in shown and "\\Office16" not in shown
