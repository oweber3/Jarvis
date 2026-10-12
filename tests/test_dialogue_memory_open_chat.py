"""Keeping an open chat's turns in the reply context however old they are.

The web chat restores a chat's recent turns into the dialogue memory when it is opened or when Jarvis
starts. While that chat stays open the model must keep the thread, even when the turns are older than the
dialogue window (``DialogueMemory.keep_last_turns``). With nothing kept, the window applies as before.
See ``webchat/webchat.spec.md`` (Chats and the shared memory).
"""
import pytest

import dialogue_clock

from jarvis.bridge.adapter import build_dialogue_context
from jarvis.bridge.settings import BridgeSettings
from jarvis.memory.conversation import DialogueMemory


@pytest.fixture
def clock(monkeypatch):
    return dialogue_clock.install(monkeypatch)


def restored(count=4) -> DialogueMemory:
    dm = DialogueMemory(inactivity_timeout=300)
    dm.set_messages([{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
                     for i in range(count)], saved=True)
    return dm


def contents(messages):
    return [m["content"] for m in messages if m.get("role") in ("user", "assistant")]


class TestTheWindowAloneByDefault:
    def test_turns_older_than_the_window_leave_the_context(self, clock):
        dm = restored()
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        assert dm.get_recent_messages() == []
        assert dm.has_recent_messages() is False


class TestKeptTurns:
    def test_restored_turns_older_than_the_window_stay_in_the_reply_context(self, clock):
        dm = restored()
        dm.keep_last_turns(20)
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        assert dm.has_recent_messages() is True
        assert contents(dm.get_recent_messages()) == ["turn 0", "turn 1", "turn 2", "turn 3"]
        assert contents(dm.get_recent_turns_with_tools()) == ["turn 0", "turn 1", "turn 2", "turn 3"]

    def test_the_cloud_reply_modes_see_the_same_thread(self, clock):
        dm = restored()
        dm.keep_last_turns(20)
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        shared = build_dialogue_context(dm, BridgeSettings(recent_dialogue_messages=6))
        assert contents(shared) == ["turn 0", "turn 1", "turn 2", "turn 3"]

    def test_a_follow_up_after_the_window_keeps_the_whole_thread_in_order(self, clock):
        dm = restored()
        dm.keep_last_turns(20)
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        dm.add_message("user", "follow-up")
        dm.add_message("assistant", "answer")
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        assert contents(dm.get_recent_messages())[-3:] == ["turn 3", "follow-up", "answer"]

    def test_only_the_last_kept_turns_survive_the_window(self, clock):
        dm = restored(count=8)
        dm.keep_last_turns(3)
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        assert contents(dm.get_recent_messages()) == ["turn 5", "turn 6", "turn 7"]

    def test_turns_inside_the_window_are_never_cut_by_the_count(self, clock):
        dm = restored(count=8)
        dm.keep_last_turns(3)
        assert len(dm.get_recent_messages()) == 8

    def test_a_diary_pass_does_not_drop_kept_turns(self, clock):
        dm = restored()
        dm.keep_last_turns(20)
        dm.add_message("user", "new question")
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        dm.mark_saved_up_to(dm.last_timestamp())
        assert contents(dm.get_recent_messages()) == ["turn 0", "turn 1", "turn 2", "turn 3", "new question"]

    def test_private_turns_stay_out_of_context_that_leaves_the_pc(self, clock):
        dm = DialogueMemory(inactivity_timeout=300)
        dm.set_messages([{"role": "user", "content": "what did I work on"},
                         {"role": "assistant", "content": "You were in Word.", "diary": False}], saved=True)
        dm.keep_last_turns(20)
        assert contents(dm.get_recent_messages(include_private=False)) == ["what did I work on"]
        assert contents(build_dialogue_context(dm, BridgeSettings())) == ["what did I work on"]

    def test_private_turns_follow_the_dialogue_window_as_before(self, clock):
        """A kept private turn would make every later reply private for as long as the chat stays open."""
        dm = DialogueMemory(inactivity_timeout=300)
        dm.set_messages([{"role": "user", "content": "what did I work on"},
                         {"role": "assistant", "content": "You were in Word.", "diary": False}], saved=True)
        dm.keep_last_turns(20)
        assert dm.has_private_context() is True
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        assert contents(dm.get_recent_messages()) == ["what did I work on"]
        assert dm.has_private_context() is False

    def test_kept_turns_alone_are_not_recent_activity(self, clock):
        """Tool results and caches reset after an idle window even while the thread is kept."""
        dm = restored()
        dm.keep_last_turns(20)
        assert dm.has_recent_activity() is True
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        assert dm.has_recent_activity() is False
        assert dm.has_recent_messages() is True
        dm.add_message("user", "back again")
        assert dm.has_recent_activity() is True

    def test_letting_go_returns_to_the_window(self, clock):
        dm = restored()
        dm.keep_last_turns(20)
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        dm.keep_last_turns(0)
        assert dm.get_recent_messages() == []
        assert dm.has_recent_messages() is False

    def test_the_setting_outlives_a_swap_of_the_conversation(self, clock):
        dm = restored()
        dm.keep_last_turns(20)
        dm.set_messages([{"role": "user", "content": "another chat"}], saved=True)
        clock.advance(dm.RECENT_WINDOW_SEC + 60)
        assert contents(dm.get_recent_messages()) == ["another chat"]


class TestSwapCursor:
    def test_a_swap_returns_the_cursor_after_which_every_turn_is_new(self):
        dm = DialogueMemory()
        dm.add_message("user", "outgoing")
        cursor = dm.set_messages([{"role": "user", "content": "restored"}], saved=True)
        dm.add_message("assistant", "lands right after the swap")
        assert [t.content for t in dm.messages_after(cursor)] == ["lands right after the swap"]
