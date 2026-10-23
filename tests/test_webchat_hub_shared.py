"""What the hub offers a second caller (phone access): a request's origin and source, a completion hook,
extra change signals, and a start/stop that counts its users. See ``remote/remote.spec.md``, One hub, two servers."""
import threading
import time

import pytest

from fake_webchat_backend import FakeWebChatBackend
from jarvis.memory.chat_store import ChatStore
from jarvis.webchat.hub import ChatHub


@pytest.fixture
def backend():
    return FakeWebChatBackend()


@pytest.fixture
def store(tmp_path):
    s = ChatStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


@pytest.fixture
def hub(backend, store):
    h = ChatHub(backend, store, sync_interval_sec=0.05)
    h.sync()
    yield h
    h.stop()


def stored(store):
    return [(m.role, m.content, m.source) for m in store.messages(store.get_active_chat_id())]


@pytest.mark.unit
class TestOriginAndSource:
    def test_a_request_goes_to_the_daemon_with_the_origin_it_was_given(self, hub, backend):
        hub.submit("hello", origin="phone")
        assert backend.origins == ["phone"]

    def test_the_web_chats_origin_is_chat(self, hub, backend):
        hub.submit("hello")
        assert backend.origins == ["chat"]

    def test_a_spoken_request_is_stored_as_voice_with_its_reply(self, hub, store):
        hub.submit("what time is it", origin="phone", source="voice")
        assert stored(store) == [("user", "what time is it", "voice"), ("assistant", "Done.", "voice")]

    def test_a_typed_request_is_stored_as_typed(self, hub, store):
        hub.submit("hello", origin="phone")
        assert [row[2] for row in stored(store)] == ["typed", "typed"]


@pytest.mark.unit
class TestFinishHook:
    def collect(self):
        seen = []
        return seen, lambda query_id, status, reply: seen.append((query_id, status, reply))

    def test_it_is_told_the_reply_when_a_request_is_answered(self, hub):
        seen, hook = self.collect()
        result = hub.submit("hello", on_finish=hook)
        assert seen == [(result.query_id, "done", "Done.")]

    def test_it_is_told_when_jarvis_was_busy(self, hub, backend):
        backend.mode = "busy"
        seen, hook = self.collect()
        result = hub.submit("hello", on_finish=hook)
        assert seen == [(result.query_id, "busy", None)]

    def test_it_is_told_when_the_request_failed(self, hub, backend):
        backend.mode = "fail"
        seen, hook = self.collect()
        result = hub.submit("hello", on_finish=hook)
        assert seen == [(result.query_id, "failed", None)]

    def test_it_is_told_when_the_request_was_stopped(self, hub, backend):
        backend.mode = "hold"
        seen, hook = self.collect()
        result = hub.submit("hello", on_finish=hook)
        assert seen == []
        hub.cancel()
        assert seen == [(result.query_id, "stopped", None)]

    def test_it_runs_after_the_reply_is_in_the_chat(self, hub, store):
        counts = []
        hub.submit("hello", on_finish=lambda *_: counts.append(len(store.messages(store.get_active_chat_id()))))
        assert counts == [2]

    def test_a_hook_that_raises_does_not_break_the_hub(self, hub):
        def explode(*_):
            raise RuntimeError("boom")

        result = hub.submit("hello", on_finish=explode)
        assert result.status == "accepted"
        assert hub.submit("again").status == "accepted"


@pytest.mark.unit
class TestExtraSignals:
    def test_a_watched_value_changing_wakes_the_poll(self, hub):
        value = {"v": 1}
        hub.watch(lambda: value["v"])
        hub.sync()
        before = hub.snapshot(0)["rev"]
        value["v"] = 2
        hub.sync()
        assert hub.snapshot(0)["rev"] > before

    def test_an_unchanged_watched_value_does_not(self, hub):
        hub.watch(lambda: 1)
        hub.sync()
        before = hub.snapshot(0)["rev"]
        hub.sync()
        assert hub.snapshot(0)["rev"] == before

    def test_a_watch_that_raises_is_ignored(self, hub):
        def explode():
            raise RuntimeError("boom")

        hub.watch(explode)
        hub.sync()

    def test_poke_wakes_a_waiting_poll(self, hub):
        rev = hub.snapshot(0)["rev"]
        woke = []
        thread = threading.Thread(target=lambda: woke.append(hub.wait(rev, 2 ** 62, timeout_sec=5)))
        thread.start()
        time.sleep(0.1)
        hub.poke()
        thread.join(timeout=2)
        assert woke and woke[0]["rev"] > rev


@pytest.mark.unit
class TestSharedLifecycle:
    def test_the_hub_keeps_running_until_its_last_user_stops(self, backend, store):
        hub = ChatHub(backend, store, sync_interval_sec=0.05)
        hub.start()
        hub.start()
        hub.stop()
        assert hub._thread is not None and hub._thread.is_alive()
        hub.stop()
        assert hub._thread is None

    def test_stopping_a_hub_that_never_started_is_harmless(self, backend, store):
        ChatHub(backend, store).stop()
