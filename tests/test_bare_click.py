"""A bare spoken "click" clicks while gesture snapping runs, without the wake word: the click routes are the only
fast routes offered to speech nobody addressed to Jarvis, and only while the mode is on."""
import time

import pytest

from tests.test_voice_latency import hear, judged, make_voice_listener


class _Running:
    running = True


@pytest.fixture
def gestures_on(monkeypatch):
    from jarvis.gestures import window_snap_service
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    monkeypatch.setitem(BUILTIN_TOOLS, "gestureControl", GestureControlTool())
    monkeypatch.setattr(window_snap_service, "_service", _Running())


def test_only_the_click_rules_are_offered_without_the_wake_word():
    import json
    from pathlib import Path
    import jarvis.fastpath
    rules = json.loads((Path(jarvis.fastpath.__file__).parent / "phrases" / "en.json").read_text("utf-8"))["rules"]
    ambient = {rule["family"] for rule in rules if rule.get("ambient")}
    assert ambient == {"gesture_click"}
    assert not any(rule.get("needs_wake_word") for rule in rules if rule.get("ambient"))


@pytest.mark.parametrize("text,action", [("click", "click"), ("double click", "double_click"),
                                         ("right click", "right_click")])
def test_an_unaddressed_click_matches_while_gesture_snapping_runs(mock_config, gestures_on, text, action):
    from jarvis.fastpath.dispatcher import match_command
    result = match_command(text, mock_config, "en", addressed=False, ambient=True)
    assert result is not None and result.args == {"action": action}


@pytest.mark.parametrize("text", ["what time is it", "pause the music", "open notepad", "paste", "start gesture control"])
def test_no_other_command_is_taken_from_unaddressed_speech(mock_config, gestures_on, text):
    from jarvis.fastpath.dispatcher import match_command
    assert match_command(text, mock_config, "en", addressed=False, ambient=True) is None


def test_an_unaddressed_click_with_gesture_snapping_off_is_ignored(mock_config, monkeypatch):
    from jarvis.fastpath.dispatcher import match_command
    from jarvis.gestures import window_snap_service
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    monkeypatch.setitem(BUILTIN_TOOLS, "gestureControl", GestureControlTool())
    monkeypatch.setattr(window_snap_service, "_service", None)
    assert match_command("click", mock_config, "en", addressed=False, ambient=True) is None


def test_the_listener_clicks_on_a_bare_click(mock_config, gestures_on):
    obj = make_voice_listener(mock_config, judged("unused"))
    hear(obj, "click", speech_end=time.time() - 0.5)
    assert obj.dispatched == ["click"]
    obj.state_manager.stop()


def test_the_listener_ignores_other_unaddressed_speech(mock_config, gestures_on):
    obj = make_voice_listener(mock_config, judged("unused"))
    hear(obj, "pause the music", speech_end=time.time() - 0.5)
    hear(obj, "click the link in the email", speech_end=time.time() - 0.5)
    assert obj.dispatched == []
    obj.state_manager.stop()


def test_the_listener_ignores_a_bare_click_while_jarvis_speaks(mock_config, gestures_on):
    from tests.test_voice_latency import FakeSpeakingTTS
    obj = make_voice_listener(mock_config, judged("unused"))
    obj.tts = FakeSpeakingTTS(speaking=True)
    hear(obj, "click", speech_end=time.time() - 0.5)
    assert obj.dispatched == []
    obj.state_manager.stop()
