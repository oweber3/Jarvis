"""A spoken click that works says nothing: no speech, no reply on the console, nothing added to the conversation.
A click that fails still says why."""
from types import SimpleNamespace

import pytest

from tests.synthetic_hands import FakeDesktop
from tests.test_window_snap_service import FakeTracker


@pytest.fixture
def gestures(mock_config, monkeypatch):
    from jarvis.gestures import window_snap_service
    from jarvis.tools.builtin.gesture_control import GestureControlTool
    from jarvis.tools.registry import BUILTIN_TOOLS
    desk = FakeDesktop()
    svc = window_snap_service.WindowSnapService(mock_config, output_factory=lambda: desk,
                                                tracker_factory=lambda camera: FakeTracker(camera))
    monkeypatch.setattr(window_snap_service, "_service", svc)
    monkeypatch.setitem(BUILTIN_TOOLS, "gestureControl", GestureControlTool())
    yield svc, desk
    svc.stop()


def test_only_the_click_rules_are_silent():
    import json
    from pathlib import Path
    import jarvis.fastpath
    rules = json.loads((Path(jarvis.fastpath.__file__).parent / "phrases" / "en.json").read_text("utf-8"))["rules"]
    assert {rule["family"] for rule in rules if rule.get("silent")} == {"gesture_click"}


def test_a_click_that_works_says_nothing(mock_config, db, dialogue_memory, gestures, capsys):
    from jarvis.reply import engine
    svc, desk = gestures
    svc.start()
    capsys.readouterr()
    spoken = []
    tts = SimpleNamespace(enabled=True, speak=spoken.append)
    engine.run_reply_engine(db, mock_config, tts, "click", dialogue_memory)
    assert desk.of("click")
    assert spoken == []
    assert "Jarvis" not in capsys.readouterr().out
    assert not dialogue_memory.has_recent_messages()


def test_a_click_that_fails_still_says_why(mock_config, db, dialogue_memory, gestures, monkeypatch):
    from jarvis.reply import engine
    svc, desk = gestures
    svc.start()
    desk.fail = True
    spoken = []
    tts = SimpleNamespace(enabled=True, speak=spoken.append)
    engine.run_reply_engine(db, mock_config, tts, "click", dialogue_memory, quiet=True)
    assert spoken and "blocked" in spoken[0].lower()
