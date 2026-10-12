"""How the built-in tools word their own steps ("Set volume to 30%", "Opened Word", "Searched the web").

A note is built from the call's arguments and outcome and carries nothing the tool specs keep out of
results: no paths, addresses, window titles or track names. See ``webchat/webchat.spec.md``, Tool steps.
"""
import pytest

from jarvis.tools.builtin.web_search import WebSearchTool
from jarvis.tools.builtin.windows.desktop_control import AppControlTool
from jarvis.tools.builtin.windows.media_control import MediaControlTool
from jarvis.tools.builtin.windows.system_volume import SystemVolumeTool
from jarvis.tools.types import StepNote, ToolExecutionResult

OK = ToolExecutionResult(success=True, reply_text="ok")


@pytest.mark.parametrize("args,label", [
    ({"action": "set", "percent": 30}, "Set the volume to 30%"),
    ({"action": "set", "percent": 29.6}, "Set the volume to 30%"),
    ({"action": "up"}, "Turned the volume up"),
    ({"action": "down", "amount": 5}, "Turned the volume down"),
    ({"action": "mute"}, "Muted the audio"),
    ({"action": "unmute"}, "Unmuted the audio"),
    ({"action": "get"}, "Checked the volume"),
])
def test_volume_steps_say_what_changed(args, label):
    assert SystemVolumeTool().step_note(args, OK) == StepNote(label)


@pytest.mark.parametrize("args,label", [
    ({"action": "open", "target": "Word"}, "Opened Word"),
    ({"action": "focus", "target": "Spotify"}, "Switched to Spotify"),
    ({"action": "close", "target": "Chrome"}, "Closed Chrome"),
    ({"action": "list", "target": ""}, "Listed the open windows"),
    ({"action": "open", "target": "12345"}, "Opened an app"),
    ({"action": "close", "target": "0x1F4"}, "Closed a window"),
])
def test_app_steps_name_the_app_but_never_a_window_handle(args, label):
    assert AppControlTool().step_note(args, OK) == StepNote(label)


@pytest.mark.parametrize("action,label", [
    ("pause", "Paused playback"), ("play", "Resumed playback"), ("play_pause", "Toggled play and pause"),
    ("next", "Skipped to the next track"), ("previous", "Went back a track"),
    ("now_playing", "Checked what is playing"),
])
def test_media_steps_never_name_the_track(action, label):
    assert MediaControlTool().step_note({"action": action}, ToolExecutionResult(True, "Now playing: A Song")) == StepNote(label)


def test_a_web_search_step_counts_the_results_without_quoting_them():
    text = ("Web search results:\n1. **First**\n   Link: https://a.example\n\n2. **Second**\n"
            "   Link: https://b.example\n\n3. **Third**\n   Link: https://c.example\n")
    note = WebSearchTool().step_note({"query": "rome"}, ToolExecutionResult(True, text))
    assert note == StepNote("Searched the web", "3 results")
    assert "rome" not in note.label.lower()


def test_a_web_search_with_no_countable_results_has_no_summary():
    assert WebSearchTool().step_note({"query": "x"}, ToolExecutionResult(True, "blocked")) == StepNote("Searched the web")


def test_one_result_is_singular():
    text = "1. **Only**\n   Link: https://a.example\n"
    assert WebSearchTool().step_note({"query": "x"}, ToolExecutionResult(True, text)).summary == "1 result"
