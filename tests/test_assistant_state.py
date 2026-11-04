"""Assistant state changes reach in-process subscribers; core never reaches into the desktop app."""
import pytest

from jarvis import assistant_state
from jarvis.assistant_state import AssistantState


@pytest.fixture(autouse=True)
def _fresh_state():
    assistant_state.reset()
    yield
    assistant_state.reset()


@pytest.mark.unit
class TestAssistantState:
    def test_starts_asleep(self):
        assert assistant_state.current() is AssistantState.ASLEEP

    def test_subscribers_hear_each_change(self):
        heard = []
        assistant_state.subscribe(heard.append)
        assistant_state.set_state(AssistantState.LISTENING)
        assistant_state.set_state(AssistantState.THINKING)
        assert heard == [AssistantState.LISTENING, AssistantState.THINKING]
        assert assistant_state.current() is AssistantState.THINKING

    def test_repeating_the_current_state_is_not_a_change(self):
        heard = []
        assistant_state.subscribe(heard.append)
        assistant_state.set_state(AssistantState.IDLE)
        assistant_state.set_state(AssistantState.IDLE)
        assert heard == [AssistantState.IDLE]

    def test_unsubscribing_stops_notifications(self):
        heard = []
        unsubscribe = assistant_state.subscribe(heard.append)
        unsubscribe()
        assistant_state.set_state(AssistantState.SPEAKING)
        assert heard == []

    def test_a_failing_subscriber_does_not_stop_the_others(self):
        heard = []

        def broken(_state):
            raise RuntimeError("boom")

        assistant_state.subscribe(broken)
        assistant_state.subscribe(heard.append)
        assistant_state.set_state(AssistantState.SPEAKING)
        assert heard == [AssistantState.SPEAKING]

    def test_every_state_has_a_desktop_face_equivalent(self):
        from desktop_app.face_widget import JarvisState
        assert {s.value for s in AssistantState} == {s.value for s in JarvisState}


@pytest.mark.unit
def test_replace_state_changes_only_from_the_expected_state():
    assistant_state.reset()
    seen = []
    assistant_state.subscribe(seen.append)
    assistant_state.set_state(AssistantState.IDLE)

    assert assistant_state.replace_state(AssistantState.IDLE, AssistantState.LISTENING) is True
    assert assistant_state.replace_state(AssistantState.IDLE, AssistantState.THINKING) is False

    assert assistant_state.current() is AssistantState.LISTENING
    assert seen == [AssistantState.IDLE, AssistantState.LISTENING]
    assistant_state.reset()


@pytest.mark.unit
def test_no_module_under_jarvis_imports_the_desktop_app():
    import ast
    from pathlib import Path

    root = Path(assistant_state.__file__).resolve().parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
            if any(name == "desktop_app" or name.startswith("desktop_app.") for name in names):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
