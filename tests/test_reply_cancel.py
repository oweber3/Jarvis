"""Stop ends a reply that is being worked out, not just the display of it.

The cancel signal is set for the duration of one request (``reply/cancellation.py``). The engine checks it
before each model turn and each tool call, passes it to the model call so a call in flight is dropped, and
returns nothing, recording nothing, once it is set. See ``reply/reply.spec.md``, Stopping a reply.
"""
import threading
from unittest.mock import patch

import pytest

from jarvis.llm.errors import RequestCancelled
from jarvis.reply import engine as engine_mod
from jarvis.reply.cancellation import cancel_scope, check_cancelled, current_cancel
from jarvis.tools.types import ToolExecutionResult


def tool_call(name="getWeather", args=None):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": args or {}}}]}}


def content(text):
    return {"message": {"role": "assistant", "content": text}}


class TestTheScope:
    def test_no_scope_means_nothing_to_cancel(self):
        assert current_cancel() is None
        check_cancelled()  # does not raise

    def test_a_set_event_raises_the_stop(self):
        event = threading.Event()
        with cancel_scope(event):
            assert current_cancel() is event
            check_cancelled()
            event.set()
            with pytest.raises(RequestCancelled):
                check_cancelled()

    def test_the_scope_ends_with_the_request(self):
        with cancel_scope(threading.Event()):
            pass
        assert current_cancel() is None

    def test_scopes_of_other_threads_do_not_meet(self):
        mine, seen = threading.Event(), []
        with cancel_scope(mine):
            thread = threading.Thread(target=lambda: seen.append(current_cancel()))
            thread.start()
            thread.join()
        assert seen == [None]


class TestTheModelCall:
    class Backend:
        def __init__(self):
            self.calls = []

        def supports_images(self, model):
            return False

        def chat(self, model, messages, **kwargs):
            self.calls.append("chat")
            return content("plain")

        def chat_cancellable(self, model, messages, cancel, **kwargs):
            self.calls.append("chat_cancellable")
            return content("cancellable")

    def call(self, cfg, backend):
        with patch.object(engine_mod, "get_llm_backend", return_value=backend):
            return engine_mod.chat_with_messages(cfg, [{"role": "user", "content": "hi"}])

    def test_without_a_stop_signal_the_plain_call_is_used(self, mock_config):
        backend = self.Backend()
        assert self.call(mock_config, backend)["message"]["content"] == "plain"
        assert backend.calls == ["chat"]

    def test_with_a_stop_signal_the_call_that_can_be_stopped_is_used(self, mock_config):
        backend = self.Backend()
        with cancel_scope(threading.Event()):
            assert self.call(mock_config, backend)["message"]["content"] == "cancellable"
        assert backend.calls == ["chat_cancellable"]


@pytest.fixture
def run(mock_config, db, dialogue_memory):
    """Run one request with the model and tools replaced; ``chat`` and ``tools`` are the fakes."""
    mock_config.ollama_chat_model = "gpt-oss:20b"
    mock_config.llm_chat_model = "gpt-oss:20b"

    def go(chat, tools=None, event=None):
        tool_runs = []

        def run_tool(db, cfg, tool_name, tool_args, **kwargs):
            tool_runs.append(tool_name)
            return (tools or (lambda: ToolExecutionResult(True, "12C", None)))()

        chat_calls = []

        def fake_chat(*args, **kwargs):
            chat_calls.append(1)
            return chat(len(chat_calls))

        with patch.object(engine_mod, "run_tool_with_retries", side_effect=run_tool), \
             patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
             patch.object(engine_mod, "select_tools", return_value=["getWeather", "stop"]), \
             patch.object(engine_mod, "extract_search_params_for_memory", return_value={"keywords": []}):
            with cancel_scope(event or threading.Event()):
                reply = engine_mod.run_reply_engine(db=db, cfg=mock_config, tts=None,
                                                    text="how is the weather?", dialogue_memory=dialogue_memory)
        return reply, tool_runs, chat_calls, dialogue_memory

    return go


class TestAReplyThatIsStopped:
    def test_a_stop_during_a_model_call_ends_the_reply_without_running_what_it_asked_for(self, run):
        event = threading.Event()

        def chat(turn):
            event.set()  # Stop is pressed while the model is working
            return tool_call()

        reply, tool_runs, chat_calls, memory = run(chat, event=event)
        assert reply is None
        assert tool_runs == [] and chat_calls == [1]
        assert [m["role"] for m in memory.all_messages()] == []

    def test_a_stop_between_tools_ends_the_reply_before_the_next_model_call(self, run):
        event = threading.Event()

        def chat(turn):
            return tool_call() if turn == 1 else content("never reached")

        def tool():
            event.set()  # Stop is pressed while the tool runs
            return ToolExecutionResult(True, "12C", None)

        reply, tool_runs, chat_calls, _ = run(chat, tools=tool, event=event)
        assert reply is None
        assert tool_runs == ["getWeather"] and chat_calls == [1]

    def test_the_stop_raised_by_the_model_call_is_not_swallowed_by_the_engine(self, run):
        def chat(turn):
            raise RequestCancelled()

        reply, tool_runs, chat_calls, memory = run(chat)
        assert reply is None and chat_calls == [1] and memory.all_messages() == []

    def test_a_reply_nobody_stops_is_delivered_as_before(self, run):
        reply, _, _, memory = run(lambda turn: content("It is sunny."))
        assert reply and "sunny" in reply
        assert [m["role"] for m in memory.all_messages()] == ["user", "assistant"]
