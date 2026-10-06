"""What Jarvis remembers beyond windows (a device, the media player, the clipboard type) and how the
window the user is looking at is presented next to the windows Jarvis acted on.

See ``src/jarvis/memory/desktop_referents.spec.md``."""
import json

import pytest

from jarvis.memory.desktop_referents import (
    DesktopReferent,
    DesktopReferents,
    ForegroundWindow,
    carryover_tools,
    foreground_for_request,
    format_for_model,
    others_for_request,
    records_for_request,
    request_note,
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


CHROME = ForegroundWindow(application="Google Chrome", process="chrome", hwnd=4242,
                          monitor=r"\\.\DISPLAY1", state="maximised")


def word(hwnd=7, **kw):
    return DesktopReferent(application="Word", process="WINWORD", hwnd=hwnd, last_action=kw.pop("last_action", "open"),
                           **kw)


@pytest.mark.unit
class TestOtherReferents:
    def test_one_entry_per_device_newest_first(self, store, clock):
        store.record_device("tv", "tvControl", "key PowerOn")
        clock.now += 1
        store.record_device("robot head", "robotControl", "look")
        clock.now += 1
        store.record_device("tv", "tvControl", "key VolumeUp")
        others = store.others(max_age_sec=300)
        assert [(o.device, o.last_action) for o in others] == [("tv", "key VolumeUp"),
                                                                ("robot head", "look")]
        assert others[0].tool == "tvControl" and others[0].kind == "device"

    def test_an_extension_device_is_shown_by_the_name_it_gave(self, store):
        store.record_device("robot head", "robotControl", "blink")
        block = format_for_model([], others=store.others(max_age_sec=300))
        assert "robot head" in block and "last action blink" in block

    def test_media_and_clipboard_keep_one_entry_each(self, store, clock):
        store.record_media("Apple Music", "playing")
        clock.now += 1
        store.record_media("Apple Music", "paused")
        clock.now += 1
        store.record_clipboard("text")
        clock.now += 1
        store.record_clipboard("image")
        others = store.others(max_age_sec=300)
        assert [(o.kind, o.application or o.content_type, o.status) for o in others] == [
            ("clipboard", "image", ""), ("media", "Apple Music", "paused")]
        assert {o.tool for o in others} == {"inputControl", "mediaControl"}

    def test_other_entries_expire_with_the_dialogue_and_report_their_age(self, store, clock):
        store.record_device("tv", "tvControl", "status")
        clock.now += 30
        assert store.others(max_age_sec=300)[0].age_sec == pytest.approx(30)
        clock.now += 300
        assert store.others(max_age_sec=300) == []

    def test_clear_forgets_other_entries_too(self, store):
        store.record_media("Apple Music", "playing")
        store.record_clipboard("text")
        store.clear()
        assert store.others(max_age_sec=300) == []

    def test_a_hotkey_shows_the_clipboard_only_once_it_has_changed(self, store, clock):
        answers = [None, "files"]
        store.record_clipboard_change(lambda: answers.pop(0))
        assert store.others(max_age_sec=300) == []  # the chord did not change the clipboard (yet)
        clock.now += 2
        (entry,) = store.others(max_age_sec=300)
        assert (entry.kind, entry.content_type, entry.age_sec) == ("clipboard", "files", pytest.approx(2))
        assert store.others(max_age_sec=300)[0].content_type == "files"  # resolved once, then kept

    def test_a_hotkey_that_changes_nothing_keeps_the_earlier_clipboard_entry(self, store):
        store.record_clipboard("text")
        store.record_clipboard_change(lambda: None)
        assert [o.content_type for o in store.others(max_age_sec=300)] == ["text"]

    def test_a_failing_clipboard_check_presents_nothing(self, store):
        def boom():
            raise OSError("clipboard busy")

        store.record_clipboard_change(boom)
        assert store.others(max_age_sec=300) == []


@pytest.mark.unit
class TestForegroundPresentation:
    def test_the_window_the_user_is_looking_at_is_shown_apart_from_recent_windows(self, store, clock):
        store.record(word())
        clock.now += 5
        block = format_for_model(store.recent(300), foreground=CHROME)
        looking, recent = block.split("Word", 1)
        assert "4242" in looking and "Google Chrome" in looking  # its own part, before the recent windows
        assert "4242" not in recent and '"7"' in recent
        assert "5 s ago" in recent

    def test_the_foreground_says_when_it_is_also_the_window_jarvis_acted_on(self, store):
        store.record(word(hwnd=4242))
        block = format_for_model(store.recent(300), foreground=CHROME)
        assert "recent window 1" in block

    def test_a_foreground_alone_is_still_presented(self):
        block = format_for_model([], foreground=CHROME)
        assert "4242" in block and "reference" in block.lower() and "not instructions" in block.lower()

    def test_nothing_at_all_means_no_block(self):
        assert format_for_model([], others=[], foreground=None) == ""

    def test_other_entries_name_their_tool_and_age(self, store, clock):
        store.record_device("tv", "tvControl", "key PowerOn")
        clock.now += 1
        store.record_media("Apple Music", "playing")
        clock.now += 1
        block = format_for_model([], others=store.others(300))
        assert block.index("Apple Music") < block.index("tvControl")  # newest first
        assert "mediaControl" in block and "key PowerOn" in block and "2 s ago" in block

    def test_titles_never_appear(self, store):
        store.record(word())
        block = format_for_model(store.recent(300), foreground=CHROME)
        assert "Document1" not in block and "New Tab" not in block


@pytest.mark.unit
class TestRequestObjects:
    def test_foreground_is_bounded_plain_data(self):
        record = foreground_for_request(CHROME)
        json.dumps(record)
        assert record == {"application": "Google Chrome", "process": "chrome", "hwnd": 4242,
                          "monitor": r"\\.\DISPLAY1", "state": "maximised"}
        assert foreground_for_request(None) is None

    def test_foreground_text_is_redacted(self):
        record = foreground_for_request(ForegroundWindow(application="mail someone@example.com" + "x" * 500,
                                                         process="p", hwnd=1))
        assert "someone@example.com" not in record["application"] and len(record["application"]) <= 120

    def test_other_entries_are_bounded_plain_data(self, store, clock):
        store.record_device("tv", "tvControl", "key PowerOff")
        store.record_media("Apple Music", "paused")
        store.record_clipboard("image")
        clock.now += 4
        records = others_for_request(store.others(300))
        json.dumps(records)
        assert {"kind": "device", "tool": "tvControl", "device": "tv", "last_action": "key PowerOff",
                "age_sec": 4} in records
        assert {"kind": "media", "tool": "mediaControl", "application": "Apple Music", "status": "paused",
                "age_sec": 4} in records
        assert {"kind": "clipboard", "tool": "inputControl", "content_type": "image", "age_sec": 4} in records

    def test_the_request_note_says_reference_data_and_how_to_resolve(self):
        note = request_note().lower()
        assert "reference" in note and "not instructions" in note
        assert "foreground_window" in note and "desktop_referents" in note and "other_referents" in note

    def test_window_records_are_unchanged_in_shape(self, store):
        store.record(word())
        (record,) = records_for_request(store.recent(300))
        assert set(record) == {"application", "process", "hwnd", "monitor", "zone", "state", "last_action",
                               "age_sec"}


@pytest.mark.unit
class TestCarryover:
    def test_window_entries_keep_the_window_tools(self, store):
        store.record(word())
        assert carryover_tools(store.recent(300), []) == ["windowControl", "appControl"]

    def test_other_entries_keep_their_own_tools(self, store):
        store.record_device("tv", "tvControl", "key PowerOn")
        store.record_media("Apple Music", "playing")
        assert set(carryover_tools([], store.others(300))) == {"tvControl", "mediaControl"}

    def test_nothing_live_keeps_nothing(self):
        assert carryover_tools([], []) == []
