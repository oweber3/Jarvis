"""DialogueMemory.messages_after: stored turns newer than a timestamp, each seen once by a mirror."""
import pytest

from jarvis.memory.conversation import DialogueMemory


@pytest.mark.unit
class TestMessagesAfter:
    def test_returns_every_turn_from_the_start(self):
        dm = DialogueMemory()
        dm.add_message("user", "hello")
        dm.add_message("assistant", "hi")
        turns = dm.messages_after(0.0)
        assert [(t.role, t.content) for t in turns] == [("user", "hello"), ("assistant", "hi")]

    def test_returns_only_newer_turns(self):
        dm = DialogueMemory()
        dm.add_message("user", "one")
        cursor = dm.messages_after(0.0)[-1].ts
        dm.add_message("assistant", "two")
        assert [t.content for t in dm.messages_after(cursor)] == ["two"]

    def test_timestamps_increase_even_for_turns_added_together(self):
        dm = DialogueMemory()
        for i in range(5):
            dm.add_message("user", str(i))
        stamps = [t.ts for t in dm.messages_after(0.0)]
        assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)

    def test_turns_kept_out_of_the_diary_are_marked_private(self):
        dm = DialogueMemory()
        dm.add_message("user", "what was I doing", diary=False)
        (turn,) = dm.messages_after(0.0)
        assert turn.private
