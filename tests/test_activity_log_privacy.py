"""Activity data never reaches the diary or the console log: the turn that quoted it is kept out of both."""
import pytest

from jarvis.memory import activity_log as al
from jarvis.memory.conversation import DialogueMemory
from jarvis.reply import engine

SECRET = "You spent two hours in Visual Studio Code on payroll_2026.xlsx"


@pytest.fixture(autouse=True)
def clean_flag():
    al.consume_turn_private()
    yield
    al.consume_turn_private()


@pytest.fixture
def memory():
    return DialogueMemory(inactivity_timeout=300.0)


@pytest.mark.unit
class TestDialogueMemory:
    def test_private_messages_stay_in_the_hot_window_but_out_of_the_diary(self, memory):
        memory.add_message("user", "what was I doing this morning?", diary=False)
        memory.add_message("assistant", SECRET, diary=False)
        memory.add_message("user", "thanks")

        assert [m["content"] for m in memory.get_recent_messages()] == [
            "what was I doing this morning?", SECRET, "thanks"]
        chunks, _ = memory.get_pending_chunks_with_snapshot()
        assert chunks == ["User: thanks"]
        assert memory.has_pending_chunks()

    def test_a_conversation_of_only_private_messages_has_nothing_to_summarise(self, memory):
        memory.add_message("user", "what was I doing?", diary=False)
        memory.add_message("assistant", SECRET, diary=False)
        assert memory.get_pending_chunks() == []
        assert not memory.has_pending_chunks()
        assert not memory.should_update_diary()

    def test_ordinary_messages_still_reach_the_diary(self, memory):
        memory.add_message("user", "hello")
        memory.add_message("assistant", "hi")
        assert memory.get_pending_chunks() == ["User: hello", "Assistant: hi"]


@pytest.mark.unit
class TestDelivery:
    def deliver(self, memory, quiet=False, error=False):
        return engine._deliver_reply(SECRET, "what was I doing?", None, None, memory, quiet, error=error)

    def test_a_turn_that_used_the_activity_log_is_not_diarised(self, memory):
        al.mark_turn_private()
        self.deliver(memory, quiet=True)
        assert memory.get_pending_chunks() == []
        assert memory.get_recent_messages()[-1]["content"] == SECRET

    def test_the_reply_is_not_printed_to_the_console_log(self, memory, capsys):
        al.mark_turn_private()
        self.deliver(memory)
        out = capsys.readouterr().out
        assert "payroll_2026" not in out and "Visual Studio Code" not in out
        assert out.strip()  # the user still sees that a reply happened

    def test_the_mark_applies_to_one_turn_only(self, memory, capsys):
        al.mark_turn_private()
        self.deliver(memory, quiet=True)
        engine._deliver_reply("Ten past four.", "what time is it?", None, None, memory, True)
        assert memory.get_pending_chunks() == ["User: what time is it?", "Assistant: Ten past four."]

    def test_ordinary_replies_print_as_before(self, memory, capsys):
        engine._deliver_reply("Ten past four.", "time?", None, None, memory, False)
        assert "Ten past four." in capsys.readouterr().out

    def test_a_stale_mark_does_not_leak_into_the_next_request(self, mock_config, db, memory):
        al.mark_turn_private()  # a turn that never reached delivery
        engine.run_reply_engine(db, mock_config, None, "What time is it?", memory, quiet=True)
        assert memory.get_pending_chunks()[0] == "User: What time is it?"


@pytest.mark.unit
class TestSessionRestore:
    def test_a_restored_chat_session_keeps_private_messages_out_of_the_diary(self, memory):
        memory.add_message("user", "what was I doing?", diary=False)
        memory.add_message("assistant", SECRET, diary=False)
        memory.add_message("user", "thanks")
        archived = memory.all_messages()

        fresh = DialogueMemory(inactivity_timeout=300.0)
        fresh.set_messages(archived)

        assert [m["content"] for m in fresh.get_recent_messages()] == [
            "what was I doing?", SECRET, "thanks"]
        assert fresh.get_pending_chunks() == ["User: thanks"]

    def test_private_messages_do_not_pile_up_once_the_window_has_passed(self, memory):
        memory.add_message("user", "what was I doing?", diary=False)
        memory.RECENT_WINDOW_SEC = -1  # everything is now outside the window
        memory.mark_saved_up_to(0.0)
        assert memory.all_messages() == []


@pytest.mark.unit
class TestRestoreFromTheChatWindowsOwnArchive:
    """The chat window archives plain role and content pairs, so privacy is matched on content."""

    def plain(self, memory):
        return [{"role": m["role"], "content": m["content"]} for m in memory.all_messages()]

    def test_a_private_exchange_stays_out_of_the_diary_after_a_session_switch(self, memory):
        memory.add_message("user", "what was I doing?", diary=False)
        memory.add_message("assistant", SECRET, diary=False)
        memory.add_message("user", "thanks")
        archived = self.plain(memory)
        memory.clear()

        memory.set_messages(archived)

        assert memory.get_pending_chunks() == ["User: thanks"]

    def test_the_daemons_restore_path_keeps_it_private(self, memory, monkeypatch):
        from jarvis import daemon
        memory.add_message("user", "what   was I doing?", diary=False)  # redaction collapses spaces
        memory.add_message("assistant", SECRET, diary=False)
        archived = self.plain(memory)
        memory.clear()
        monkeypatch.setattr(daemon, "_global_dialogue_memory", memory)

        assert daemon.set_chat_messages(archived) is True

        assert memory.get_pending_chunks() == []
        assert len(memory.get_recent_messages()) == 2


def _activity_tool_turn():
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "a1", "type": "function", "function": {"name": "activityLog", "arguments": {}}}]},
        {"role": "tool", "tool_call_id": "a1", "tool_name": "activityLog",
         "content": '{"apps": [{"app": "Excel", "title": "Budget-2026.xlsx", "seconds": 7200}]}'},
    ]


@pytest.mark.unit
class TestFollowUps:
    """A follow-up answered from the activity data already in the context is as private as the first turn."""

    def test_private_context_is_reported_while_activity_turns_are_live(self, memory):
        assert not memory.has_private_context()
        memory.add_message("user", "what was I doing?", diary=False)
        assert memory.has_private_context()

    def test_a_private_tool_result_carried_over_keeps_the_context_private(self, memory):
        memory.record_tool_turn(_activity_tool_turn(), private=True)
        assert memory.has_private_context()

    def test_ordinary_tool_results_do_not(self, memory):
        memory.add_message("user", "hello")
        memory.record_tool_turn(_activity_tool_turn()[:1])
        assert not memory.has_private_context()

    def test_a_follow_up_reply_from_carried_over_activity_data_stays_out_of_the_diary(self, memory, capsys):
        self._follow_up(memory)
        assert all("Budget-2026" not in chunk for chunk in memory.get_pending_chunks())
        assert "Budget-2026" not in capsys.readouterr().out

    def test_debug_logs_carry_no_activity_data(self, memory, capsys, monkeypatch):
        from jarvis import debug
        monkeypatch.setattr(debug, "_is_debug_enabled", lambda: True)
        self._follow_up(memory, voice_debug=True)
        captured = capsys.readouterr()
        assert "Budget-2026" not in captured.err and "Budget-2026" not in captured.out
        assert "LLM response" in captured.err  # the log still says a response came back

    def _follow_up(self, memory, voice_debug=False):
        from unittest.mock import Mock, patch
        from test_engine_tool_carryover_guard import _mock_cfg

        memory.add_message("user", "what was I doing this afternoon?", diary=False)
        memory.record_tool_turn(_activity_tool_turn(), private=True)
        memory.add_message("assistant", "Mostly Excel.", diary=False)
        answer = "About two hours in Budget-2026.xlsx."
        with patch("jarvis.reply.engine.chat_with_messages", return_value={"message": {"content": answer}}), \
                patch("jarvis.reply.engine.extract_text_from_response", return_value=answer), \
                patch("jarvis.reply.engine.extract_search_params_for_memory", return_value={}), \
                patch("jarvis.reply.engine.plan_query", return_value=[]), \
                patch("jarvis.memory.graph.GraphMemoryStore"), \
                patch("jarvis.memory.graph_ops.build_warm_profile", return_value={"user": "", "directives": ""}), \
                patch("jarvis.memory.graph_ops.format_warm_profile_block", return_value=""), \
                patch("jarvis.reply.engine.select_tools", return_value=[]):
            cfg = _mock_cfg()
            cfg.voice_debug = voice_debug
            engine.run_reply_engine(db=Mock(), cfg=cfg, tts=None,
                                    text="how long was I in that spreadsheet?", dialogue_memory=memory)
