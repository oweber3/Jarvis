"""Switching the shared conversation between web chats, and several chat views at once.

The dialogue memory is the one conversation voice and text share. Opening another chat swaps its
contents (``daemon.switch_chat_conversation``): the outgoing conversation reaches the diary exactly
once, the incoming one is not summarised again, and nothing runs while a query holds the lock.
See ``webchat/webchat.spec.md``.
"""
import threading
import time

import pytest

from jarvis import daemon
from jarvis.memory.conversation import DialogueMemory


@pytest.fixture
def memory(monkeypatch):
    dm = DialogueMemory(inactivity_timeout=300)
    monkeypatch.setattr(daemon, "_global_dialogue_memory", dm)
    monkeypatch.setattr(daemon, "_global_cfg", object())
    monkeypatch.setattr(daemon, "_global_db", object())
    monkeypatch.setattr(daemon, "_chat_query_lock", threading.Lock())
    return dm


@pytest.fixture
def diary(monkeypatch):
    """Record what the diary pass is asked to summarise, instead of calling a model."""
    saved = []
    done = threading.Event()

    def fake_update(db, dialogue_memory, cfg, **kwargs):
        saved.append(dialogue_memory.get_pending_chunks())
        done.set()
        return 1

    monkeypatch.setattr(daemon, "update_diary_from_dialogue_memory", fake_update)
    saved_done = type("Diary", (), {})()
    saved_done.saved, saved_done.done = saved, done
    return saved_done


def contents(dm):
    return [(m["role"], m["content"]) for m in dm.all_messages()]


class TestDialogueMemoryRestoreAsSaved:
    def test_restored_turns_are_not_pending_for_the_diary(self):
        dm = DialogueMemory()
        dm.set_messages([{"role": "user", "content": "old"}, {"role": "assistant", "content": "older"}], saved=True)
        assert dm.get_pending_chunks() == []
        dm.add_message("user", "new")
        assert dm.get_pending_chunks() == ["User: new"]

    def test_restored_turns_are_pending_by_default(self):
        dm = DialogueMemory()
        dm.set_messages([{"role": "user", "content": "old"}])
        assert dm.get_pending_chunks() == ["User: old"]

    def test_detach_unsaved_copies_only_what_the_diary_has_not_seen(self):
        dm = DialogueMemory()
        dm.add_message("user", "saved already")
        dm.mark_saved_up_to(dm.last_timestamp())
        dm.add_message("user", "fresh question")
        dm.add_message("assistant", "private answer", diary=False)
        copy = dm.detach_unsaved()
        assert copy.get_pending_chunks() == ["User: fresh question"]
        assert dm.get_pending_chunks() == ["User: fresh question"]  # the original is untouched

    def test_last_timestamp_is_the_newest_turn(self):
        dm = DialogueMemory()
        assert dm.last_timestamp() == 0.0
        dm.add_message("user", "hi")
        first = dm.last_timestamp()
        dm.add_message("assistant", "hello")
        assert dm.last_timestamp() > first
        assert [t.content for t in dm.messages_after(first)] == ["hello"]


class TestSwitchChatConversation:
    def test_replaces_the_conversation_with_the_chosen_chats_turns(self, memory, diary):
        memory.add_message("user", "about cooking")
        assert daemon.switch_chat_conversation([
            {"role": "user", "content": "about trains"}, {"role": "assistant", "content": "ok"}]) is True
        assert contents(memory) == [("user", "about trains"), ("assistant", "ok")]

    def test_the_outgoing_conversation_reaches_the_diary_once(self, memory, diary):
        memory.add_message("user", "about cooking")
        memory.add_message("assistant", "pasta")
        daemon.switch_chat_conversation([])
        assert diary.done.wait(5)
        assert diary.saved == [["User: about cooking", "Assistant: pasta"]]

    def test_a_conversation_the_diary_already_has_is_not_summarised_again(self, memory, diary):
        memory.add_message("user", "about cooking")
        memory.mark_saved_up_to(memory.last_timestamp())
        daemon.switch_chat_conversation([])
        time.sleep(0.2)
        assert diary.saved == []

    def test_the_incoming_conversation_is_not_summarised(self, memory, diary):
        daemon.switch_chat_conversation([{"role": "user", "content": "old chat"}])
        time.sleep(0.2)
        assert diary.saved == []
        assert memory.get_pending_chunks() == []

    def test_new_turns_after_a_switch_are_pending(self, memory, diary):
        daemon.switch_chat_conversation([{"role": "user", "content": "old chat"}])
        memory.add_message("user", "a follow-up")
        assert memory.get_pending_chunks() == ["User: a follow-up"]

    def test_turns_are_redacted_on_the_way_in(self, memory, diary):
        daemon.switch_chat_conversation([{"role": "user", "content": "my email is jane.doe@example.com"}])
        assert "jane.doe@example.com" not in contents(memory)[0][1]

    def test_private_turns_stay_out_of_the_diary_and_out_of_cloud_context(self, memory, diary):
        daemon.switch_chat_conversation([
            {"role": "assistant", "content": "you spent 2h in Code", "diary": False},
            {"role": "user", "content": "thanks"}])
        assert [m["content"] for m in memory.get_recent_messages(include_private=False)] == ["thanks"]

    def test_refused_while_a_query_is_running(self, memory, diary):
        memory.add_message("user", "keep me")
        daemon._chat_query_lock.acquire()
        try:
            assert daemon.switch_chat_conversation([]) is False
        finally:
            daemon._chat_query_lock.release()
        assert contents(memory) == [("user", "keep me")]

    def test_refused_before_the_daemon_has_booted(self, monkeypatch):
        monkeypatch.setattr(daemon, "_global_dialogue_memory", None)
        assert daemon.switch_chat_conversation([]) is False

    def test_the_lock_is_free_afterwards(self, memory, diary):
        daemon.switch_chat_conversation([])
        assert daemon._chat_query_lock.acquire(blocking=False)
        daemon._chat_query_lock.release()


class TestChatResultListeners:
    @pytest.fixture(autouse=True)
    def _clean(self, memory):
        daemon._chat_result_listeners.clear()
        yield
        daemon._chat_result_listeners.clear()

    def test_every_registered_view_receives_a_confirmed_result(self, memory):
        first, second = [], []
        daemon.add_chat_result_listener(first.append)
        daemon.add_chat_result_listener(second.append)
        daemon.deliver_chat_confirmed_result("Done.", True)
        assert first == ["Done."] and second == ["Done."]
        assert contents(memory) == [("assistant", "Done.")]

    def test_a_removed_view_stops_receiving(self, memory):
        got = []
        daemon.add_chat_result_listener(got.append)
        daemon.remove_chat_result_listener(got.append)
        daemon.deliver_chat_confirmed_result("Done.", True)
        assert got == []

    def test_a_failing_view_does_not_stop_the_others(self, memory):
        got = []

        def broken(_reply):
            raise RuntimeError("window destroyed")

        daemon.add_chat_result_listener(broken)
        daemon.add_chat_result_listener(got.append)
        daemon.deliver_chat_confirmed_result("Done.", True)
        assert got == ["Done."]

    def test_removing_a_view_that_was_never_added_is_harmless(self):
        daemon.remove_chat_result_listener(print)
