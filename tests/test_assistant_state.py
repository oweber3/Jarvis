"""Assistant state changes reach in-process subscribers and the desktop face, in that order of independence."""
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

    def test_the_desktop_face_is_told_even_when_nothing_subscribes(self, monkeypatch):
        import desktop_app.face_widget as face_widget

        told = []

        class Face:
            def set_state(self, state):
                told.append(state)

        monkeypatch.setattr(face_widget, "get_jarvis_state", lambda: Face())
        assistant_state.set_state(AssistantState.THINKING)
        assert told == [face_widget.JarvisState.THINKING]

    def test_a_missing_desktop_face_never_blocks_subscribers(self, monkeypatch):
        import desktop_app.face_widget as face_widget

        def unavailable():
            raise RuntimeError("no Qt here")

        monkeypatch.setattr(face_widget, "get_jarvis_state", unavailable)
        heard = []
        assistant_state.subscribe(heard.append)
        assistant_state.set_state(AssistantState.LISTENING)
        assert heard == [AssistantState.LISTENING]

    def test_every_state_has_a_desktop_face_equivalent(self):
        from desktop_app.face_widget import JarvisState
        assert {s.value for s in AssistantState} == {s.value for s in JarvisState}
