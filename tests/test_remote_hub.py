"""RemoteHub: the phone's view of the one conversation, its queries and Jarvis's state."""
import threading
import time

import pytest

from fake_remote_backend import FakeBackend
from jarvis.remote.hub import RemoteHub


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def hub(backend):
    hub = RemoteHub(backend, sync_interval_sec=0.05)
    yield hub
    hub.stop()


def texts(snapshot, role=None):
    return [e["text"] for e in snapshot["entries"] if role is None or e["role"] == role]


@pytest.mark.unit
class TestConversationMirror:
    def test_turns_already_in_the_conversation_are_shown(self, backend, hub):
        backend.memory.add_message("user", "what time is it")
        backend.memory.add_message("assistant", "It's noon.")
        hub.sync()
        assert texts(hub.snapshot(after=0)) == ["what time is it", "It's noon."]

    def test_voice_turns_appear_as_they_happen(self, backend, hub):
        hub.sync()
        backend.memory.add_message("user", "pause the music")
        backend.memory.add_message("assistant", "Paused.")
        hub.sync()
        assert texts(hub.snapshot(after=0)) == ["pause the music", "Paused."]

    def test_each_turn_is_shown_once(self, backend, hub):
        backend.memory.add_message("user", "hello")
        hub.sync()
        hub.sync()
        assert texts(hub.snapshot(after=0)) == ["hello"]

    def test_only_entries_after_the_cursor_are_returned(self, backend, hub):
        backend.memory.add_message("user", "one")
        hub.sync()
        cursor = hub.snapshot(after=0)["entries"][-1]["id"]
        backend.memory.add_message("assistant", "two")
        hub.sync()
        assert texts(hub.snapshot(after=cursor)) == ["two"]

    def test_entries_outlive_the_conversation_timeout(self, backend, hub):
        backend.memory.add_message("user", "remember this")
        hub.sync()
        backend.memory.clear()
        hub.sync()
        assert texts(hub.snapshot(after=0)) == ["remember this"]

    def test_the_log_keeps_only_the_newest_entries(self, backend):
        hub = RemoteHub(backend, max_entries=3)
        for i in range(5):
            backend.memory.add_message("user", str(i))
        hub.sync()
        assert texts(hub.snapshot(after=0)) == ["2", "3", "4"]

    def test_no_memory_yet_shows_nothing(self, backend, hub):
        backend.memory = None
        hub.sync()
        assert hub.snapshot(after=0)["entries"] == []

    def test_the_hub_syncs_by_itself_once_started(self, backend, hub):
        hub.start()
        backend.memory.add_message("user", "background turn")
        deadline = time.time() + 2
        while time.time() < deadline and not texts(hub.snapshot(after=0)):
            time.sleep(0.02)
        assert texts(hub.snapshot(after=0)) == ["background turn"]


@pytest.mark.unit
class TestPhoneQueries:
    def test_a_reply_is_in_the_conversation_when_the_query_is_done(self, backend, hub):
        result = hub.submit("volume up")
        assert result.status == "accepted"
        snap = hub.snapshot(after=0)
        assert snap["queries"][str(result.query_id)] == {"status": "done", "reply": "Done."}
        assert texts(snap) == ["volume up", "Done."]

    def test_a_busy_jarvis_is_reported(self, backend, hub):
        backend.mode = "busy"
        result = hub.submit("hello")
        assert result.status == "busy"
        snap = hub.snapshot(after=0)
        assert snap["queries"][str(result.query_id)]["status"] == "busy"
        assert texts(snap, "notice") == ["Jarvis is busy with another request."]

    def test_an_unready_daemon_is_reported(self, backend, hub):
        backend.mode = "unavailable"
        assert hub.submit("hello").status == "unavailable"

    def test_a_failed_reply_keeps_the_request_and_says_so(self, backend, hub):
        backend.mode = "fail"
        result = hub.submit("do the thing")
        snap = hub.snapshot(after=0)
        assert snap["queries"][str(result.query_id)]["status"] == "failed"
        assert texts(snap) == ["do the thing", "Jarvis couldn't answer that."]

    def test_jarvis_reads_as_thinking_while_a_phone_query_runs(self, backend, hub):
        backend.mode = "hold"
        result = hub.submit("think hard")
        assert hub.snapshot(after=0)["state"] == "thinking"
        assert hub.snapshot(after=0)["queries"][str(result.query_id)]["status"] == "pending"
        backend.release()
        assert hub.snapshot(after=0)["state"] == "idle"

    def test_stop_cancels_the_query_and_says_so(self, backend, hub):
        backend.mode = "hold"
        result = hub.submit("long task")
        hub.cancel()
        snap = hub.snapshot(after=0)
        assert backend.cancelled == 1
        assert snap["queries"][str(result.query_id)]["status"] == "stopped"
        assert texts(snap, "notice") == ["Stopped."]

    def test_only_recent_queries_are_kept(self, backend):
        hub = RemoteHub(backend, max_queries=2)
        ids = [hub.submit(f"q{i}").query_id for i in range(4)]
        assert set(hub.snapshot(after=0)["queries"]) == {str(i) for i in ids[-2:]}


@pytest.mark.unit
class TestStatusAndWaiting:
    def test_the_snapshot_carries_state_mode_busy_and_pc_use(self, backend, hub):
        backend.state = "listening"
        backend.busy = True
        snap = hub.snapshot(after=0)
        assert snap["state"] == "listening"
        assert snap["busy"] is True
        assert snap["mode"] == backend.reply_mode
        assert snap["pc"] == backend.stats

    def test_a_pending_confirmation_is_shown_and_can_be_answered(self, backend, hub):
        backend.confirmation = {"id": "abc", "tool": "fileOps", "action": "delete", "target": "notes.txt",
                                "consequence": "The file is removed.", "expires_in": 40}
        assert hub.snapshot(after=0)["confirmation"]["id"] == "abc"
        assert hub.resolve_confirmation("abc", True)
        assert backend.resolved == [("abc", True)]
        assert hub.snapshot(after=0)["confirmation"] is None

    def test_a_stale_confirmation_answer_changes_nothing(self, backend, hub):
        assert not hub.resolve_confirmation("gone", True)

    def test_a_wait_returns_at_once_when_something_is_newer(self, backend, hub):
        backend.memory.add_message("user", "hi")
        hub.sync()
        started = time.time()
        snap = hub.wait(rev=-1, after=0, timeout_sec=5)
        assert time.time() - started < 1
        assert texts(snap) == ["hi"]

    def test_a_wait_wakes_on_a_state_change(self, backend, hub):
        hub.start()
        rev = hub.snapshot(after=0)["rev"]
        threading.Timer(0.2, backend.set_state, args=("listening",)).start()
        started = time.time()
        snap = hub.wait(rev=rev, after=0, timeout_sec=5)
        assert time.time() - started < 2
        assert snap["state"] == "listening"

    def test_a_wait_with_nothing_new_times_out_with_the_current_snapshot(self, backend, hub):
        hub.sync()
        snap = hub.snapshot(after=0)
        started = time.time()
        again = hub.wait(rev=snap["rev"], after=0, timeout_sec=0.3)
        assert 0.25 <= time.time() - started < 2
        assert again["rev"] == snap["rev"]
