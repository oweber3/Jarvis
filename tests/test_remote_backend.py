"""DaemonBackend: what phone access sees of the real daemon, confirmation store and assistant state."""
import pytest

from jarvis import assistant_state
from jarvis.remote.backend import DaemonBackend
from jarvis.tools.confirmation import ConfirmationRequest, ConfirmationStore, SafetyTier


def request(tier=SafetyTier.CONFIRM_DIALOG):
    return ConfirmationRequest(tool_name="fileOps", tier=tier, action="delete", target="notes.txt",
                               parameters={"path": "notes.txt"}, consequence="The file is removed.")


class Handle:
    closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def store():
    store = ConfirmationStore()
    yield store
    store.clear_pending()


@pytest.mark.unit
class TestConfirmations:
    def test_a_desktop_dialog_request_is_described(self, store):
        req = request()
        store.begin_dialog(req, {}, lambda r, resolve: Handle())
        pending = DaemonBackend(confirmation_store=store).pending_confirmation()
        assert pending["id"] == req.id
        assert (pending["tool"], pending["action"], pending["target"]) == ("fileOps", "delete", "notes.txt")
        assert pending["consequence"] == "The file is removed."
        assert 0 < pending["expires_in"] <= 60

    def test_a_spoken_yes_or_no_request_is_not_offered(self, store):
        store.set_pending(request(SafetyTier.CONFIRM_VOICE))
        assert DaemonBackend(confirmation_store=store).pending_confirmation() is None

    def test_denying_from_the_phone_closes_the_desktop_dialog(self, store):
        req, handle = request(), Handle()
        store.begin_dialog(req, {}, lambda r, resolve: handle)
        assert DaemonBackend(confirmation_store=store).resolve_confirmation(req.id, False) is True
        assert handle.closed
        assert store.get_pending() is None

    def test_approving_from_the_phone_schedules_the_action(self, store, monkeypatch):
        scheduled = []
        monkeypatch.setattr(store, "schedule", lambda action, **kw: scheduled.append(action.request.id))
        req, handle = request(), Handle()
        store.begin_dialog(req, {}, lambda r, resolve: handle)
        assert DaemonBackend(confirmation_store=store).resolve_confirmation(req.id, True) is True
        assert scheduled == [req.id] and handle.closed

    def test_a_stale_answer_is_ignored(self, store):
        assert DaemonBackend(confirmation_store=store).resolve_confirmation("gone", True) is False


@pytest.mark.unit
class TestDaemonState:
    def test_the_assistant_state_is_read_and_followed(self):
        assistant_state.reset()
        seen = []
        backend = DaemonBackend()
        unsubscribe = backend.subscribe_state(seen.append)
        try:
            assistant_state.set_state(assistant_state.AssistantState.LISTENING)
            assert backend.assistant_state() == "listening"
            assert seen == ["listening"]
        finally:
            unsubscribe()
            assistant_state.reset()

    def test_pc_use_is_in_percent(self):
        stats = DaemonBackend().pc_stats()
        if stats is not None:
            assert 0 <= stats["cpu"] <= 100 and 0 <= stats["ram"] <= 100

    def test_a_daemon_that_has_not_booted_answers_none(self, monkeypatch):
        from jarvis import daemon
        monkeypatch.setattr(daemon, "_global_dialogue_memory", None)
        completed = []
        DaemonBackend().submit("hello", on_start=lambda q: None, on_complete=completed.append,
                               on_busy=lambda: None)
        assert completed == [None]
