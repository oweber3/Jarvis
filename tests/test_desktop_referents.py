"""The desktop referent store: what Jarvis remembers about the windows it just acted on."""
import json

import pytest

from jarvis.memory.desktop_referents import (
    DesktopReferent,
    DesktopReferents,
    format_for_model,
    records_for_request,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return DesktopReferents(clock=clock)


def window(application, hwnd, **kw):
    return DesktopReferent(application=application, process=kw.pop("process", application), hwnd=hwnd, **kw)


@pytest.mark.unit
class TestRecording:
    def test_newest_first_and_bounded(self, store, clock):
        for n in range(DesktopReferents.MAX_ENTRIES + 2):
            clock.now += 1
            store.record(window(f"App{n}", 100 + n, last_action="focus"))
        recent = store.recent(max_age_sec=300)
        assert len(recent) == DesktopReferents.MAX_ENTRIES
        assert [r.hwnd for r in recent] == [100 + n for n in range(DesktopReferents.MAX_ENTRIES + 1, 1, -1)]

    def test_a_later_action_on_the_same_window_updates_it_and_moves_it_first(self, store, clock):
        store.record(window("Word", 7, monitor=r"\\.\DISPLAY2", zone="left", state="normal", last_action="place"))
        clock.now += 1
        store.record(window("Apple Music", 9, last_action="open"))
        clock.now += 1
        store.record(DesktopReferent(application="", hwnd=7, state="maximised", last_action="maximise"))
        first, second = store.recent(max_age_sec=300)
        assert first.hwnd == 7 and second.hwnd == 9
        # The display is kept, the zone no longer applies once maximised, the name is not lost.
        assert (first.application, first.monitor, first.zone, first.state) == (
            "Word", r"\\.\DISPLAY2", "", "maximised")

    def test_a_window_of_the_launched_application_replaces_its_pending_launch(self, store):
        store.record(DesktopReferent(application="Word", process="WINWORD", last_action="open"))
        store.record(window("Word", 7, process="WINWORD", monitor=r"\\.\DISPLAY2", last_action="place"))
        assert [(r.application, r.hwnd) for r in store.recent(max_age_sec=300)] == [("Word", 7)]

    def test_a_new_launch_replaces_an_older_pending_launch_of_the_same_application(self, store):
        store.record(DesktopReferent(application="Notepad", process="notepad", last_action="open"))
        store.record(DesktopReferent(application="Notepad", process="notepad", last_action="open"))
        assert len(store.recent(max_age_sec=300)) == 1

    def test_entries_expire(self, store, clock):
        store.record(window("Word", 7, last_action="open"))
        clock.now += 301
        assert store.recent(max_age_sec=300) == []

    def test_ages_are_reported(self, store, clock):
        store.record(window("Word", 7, last_action="open"))
        clock.now += 12
        assert store.recent(max_age_sec=300)[0].age_sec == pytest.approx(12)

    def test_clear_forgets_everything(self, store):
        store.record(window("Word", 7, last_action="open"))
        store.clear()
        assert store.recent(max_age_sec=300) == []


@pytest.mark.unit
class TestPendingLaunches:
    def test_a_pending_launch_resolves_to_its_window_once(self, store):
        calls = []

        def resolve():
            calls.append(1)
            return {"hwnd": 42, "process": "WINWORD", "monitor": r"\\.\DISPLAY1"}

        store.record(DesktopReferent(application="Word", process="WINWORD", last_action="open"), resolve=resolve)
        for _ in range(3):
            (entry,) = store.recent(max_age_sec=300)
            assert (entry.hwnd, entry.monitor, entry.application) == (42, r"\\.\DISPLAY1", "Word")
        assert len(calls) == 1

    def test_an_unresolved_launch_stays_pending_and_is_retried(self, store):
        answers = [None, {"hwnd": 42, "process": "WINWORD"}]
        store.record(DesktopReferent(application="Word", process="WINWORD", last_action="open"),
                     resolve=lambda: answers.pop(0))
        assert store.recent(max_age_sec=300)[0].hwnd is None
        assert store.recent(max_age_sec=300)[0].hwnd == 42

    def test_a_failing_resolver_leaves_the_launch_pending(self, store):
        def boom():
            raise OSError("listing failed")

        store.record(DesktopReferent(application="Word", process="WINWORD", last_action="open"), resolve=boom)
        assert store.recent(max_age_sec=300)[0].hwnd is None


@pytest.mark.unit
class TestPresentation:
    def test_nothing_to_say_without_referents(self):
        assert format_for_model([]) == ""
        assert records_for_request([]) == []

    def test_model_block_names_the_newest_window_first_with_its_handle(self, store, clock):
        store.record(window("Notepad", 5, process="notepad", last_action="open"))
        clock.now += 1
        store.record(window("Word", 131338, process="WINWORD", monitor=r"\\.\DISPLAY2", state="maximised",
                            last_action="maximise"))
        block = format_for_model(store.recent(max_age_sec=300))
        assert block.index("131338") < block.index("Notepad")
        assert "WINWORD" in block and r"\\.\DISPLAY2" in block and "maximised" in block
        assert "reference" in block.lower() and "not instructions" in block.lower()

    def test_a_pending_launch_says_its_window_is_not_found_yet(self, store):
        store.record(DesktopReferent(application="Word", process="WINWORD", last_action="open"))
        block = format_for_model(store.recent(max_age_sec=300))
        assert "Word" in block and "not found yet" in block

    def test_request_records_are_bounded_redacted_plain_data(self, store, clock):
        store.record(window("Word", 7, process="WINWORD", monitor=r"\\.\DISPLAY2", zone="left", state="normal",
                            last_action="place"))
        store.record(DesktopReferent(application="mail me at someone@example.com " + "x" * 500, last_action="open"))
        clock.now += 3
        records = records_for_request(store.recent(max_age_sec=300))
        json.dumps(records)  # plain JSON
        assert set(records[1]) == {"application", "process", "hwnd", "monitor", "zone", "state", "last_action",
                                   "age_sec"}
        assert records[1]["hwnd"] == 7 and records[1]["age_sec"] == 3
        assert "someone@example.com" not in records[0]["application"]
        assert len(records[0]["application"]) <= 120
