"""The web chat hub: what lands in which chat, how chats are opened, and what the page is told.

The daemon is replaced by a fake with a real dialogue memory; storage is a real ``ChatStore`` in a
temporary file. See ``webchat/webchat.spec.md``.
"""
import threading
import time

import pytest

from fake_webchat_backend import FakeWebChatBackend
from jarvis.memory.chat_store import ANY_PROJECT, ChatStore
from jarvis.webchat.hub import DEFAULT_CHAT_TITLE, ChatHub


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


def transcript(store, chat_id):
    return [(m.role, m.content, m.source) for m in store.messages(chat_id)]


def open_id(store):
    return store.get_active_chat_id()


class TestFirstRun:
    def test_a_first_run_opens_a_chat_for_voice_and_quick_questions(self, hub, store):
        chats = store.list_chats(ANY_PROJECT)
        assert [c.title for c in chats] == [DEFAULT_CHAT_TITLE]
        assert open_id(store) == chats[0].id

    def test_the_last_open_chat_stays_open_after_a_restart(self, backend, store):
        first = ChatHub(backend, store)
        first.sync()
        chat = first.new_chat().chat
        again = ChatHub(backend, store)
        again.sync()
        assert open_id(store) == chat.id

    def test_a_restart_gives_the_model_the_open_chats_history_again(self, backend, store):
        first = ChatHub(backend, store)
        first.sync()
        first.submit("what is a nebula")
        backend.memory = type(backend.memory)()  # the daemon restarted with an empty memory
        again = ChatHub(backend, store)
        again.sync()
        assert [m["content"] for m in backend.memory.all_messages()] == ["what is a nebula", "Done."]
        assert backend.memory.get_pending_chunks() == []

    def test_nothing_is_restored_into_a_conversation_already_under_way(self, backend, store):
        first = ChatHub(backend, store)
        first.sync()
        first.submit("old question")
        backend.memory = type(backend.memory)()
        backend.speak("fresh voice question", "fresh answer")
        again = ChatHub(backend, store)
        again.sync()
        assert [m["content"] for m in backend.memory.all_messages()] == ["fresh voice question", "fresh answer"]


class TestTypedMessages:
    def test_a_typed_exchange_is_stored_in_the_open_chat(self, hub, store):
        result = hub.submit("hello there")
        assert result.status == "accepted"
        assert transcript(store, open_id(store)) == [
            ("user", "hello there", "typed"), ("assistant", "Done.", "typed")]

    def test_the_first_message_titles_the_chat(self, hub, store):
        hub.new_chat()
        hub.submit("Plan my week please")
        assert store.get_chat(open_id(store)).title == "Plan my week please"

    def test_sending_with_no_open_chat_starts_one(self, hub, store):
        hub.delete_chat(open_id(store))
        assert open_id(store) is None
        assert hub.submit("hello").status == "accepted"
        assert transcript(store, open_id(store))[0][:2] == ("user", "hello")

    def test_the_mode_and_model_used_are_remembered_by_the_chat(self, hub, backend, store):
        backend.reply_mode = {"mode": "claude", "enabled": ["local", "claude"]}
        hub.submit("hi")
        chat = store.get_chat(open_id(store))
        assert (chat.last_mode, chat.last_model) == ("claude", "gemma4:12b")

    def test_a_spoken_answer_updates_what_the_chat_last_used(self, hub, backend, store):
        backend.reply_mode = {"mode": "claude", "enabled": ["local", "claude"]}
        backend.speak("what time is it", "Ten past three")
        hub.sync()
        chat = store.get_chat(open_id(store))
        assert (chat.last_mode, chat.last_model) == ("claude", "gemma4:12b")

    def test_a_busy_jarvis_stores_nothing_and_says_so(self, hub, backend, store):
        backend.mode = "busy"
        result = hub.submit("hello")
        assert result.status == "busy"
        assert store.messages(open_id(store)) == []
        assert [n["kind"] for n in hub.snapshot(0)["notices"]] == ["busy"]

    def test_a_failed_request_is_a_notice_not_a_stored_reply(self, hub, backend, store):
        backend.mode = "fail"
        hub.submit("hello")
        assert store.messages(open_id(store)) == []
        notices = hub.snapshot(0)["notices"]
        assert [n["kind"] for n in notices] == ["failed"] and notices[0]["request"] == "hello"

    def test_a_daemon_that_is_not_ready_is_reported_as_unavailable(self, hub, backend):
        backend.mode = "unavailable"
        assert hub.submit("hello").status == "unavailable"

    def test_stopping_a_request_in_flight_leaves_a_stopped_notice(self, hub, backend, store):
        backend.mode = "hold"
        hub.submit("slow one")
        hub.cancel()
        assert backend.cancelled == 1
        assert [n["kind"] for n in hub.snapshot(0)["notices"]] == ["stopped"]
        assert store.messages(open_id(store)) == []

    def test_the_page_can_read_a_late_reply_after_it_is_released(self, hub, backend, store):
        backend.mode = "hold"
        hub.submit("slow one")
        assert hub.snapshot(0)["busy_query"] is True
        backend.release()
        assert hub.snapshot(0)["busy_query"] is False
        assert transcript(store, open_id(store))[-1][:2] == ("assistant", "Done.")


class TestVoiceAndConfirmations:
    def test_a_spoken_exchange_joins_the_open_chat(self, hub, backend, store):
        backend.speak("what time is it", "Ten past three")
        hub.sync()
        assert transcript(store, open_id(store)) == [
            ("user", "what time is it", "voice"), ("assistant", "Ten past three", "voice")]

    def test_a_turn_is_mirrored_once(self, hub, backend, store):
        backend.speak("hi", "hello")
        hub.sync()
        hub.sync()
        assert len(store.messages(open_id(store))) == 2

    def test_the_outcome_of_a_confirmed_action_is_marked_as_such(self, hub, backend, store):
        hub.submit("uninstall the app")
        backend.memory.add_message("assistant", "Uninstalled the app.")
        hub.sync()
        assert transcript(store, open_id(store))[-1] == ("assistant", "Uninstalled the app.", "confirmed")

    def test_a_turn_kept_private_stays_private_in_the_chat(self, hub, backend, store):
        backend.memory.add_message("user", "what was I doing", diary=False)
        backend.memory.add_message("assistant", "You were in Code", diary=False)
        hub.sync()
        assert [m.private for m in store.messages(open_id(store))] == [True, True]


class TestOpeningChats:
    def test_opening_a_chat_gives_the_model_that_chats_conversation(self, hub, backend, store):
        hub.submit("about trains")
        trains = open_id(store)
        hub.new_chat()
        hub.submit("about boats")
        assert hub.open_chat(trains).status == "ok"
        assert [m["content"] for m in backend.memory.all_messages()] == ["about trains", "Done."]
        assert open_id(store) == trains

    def test_voice_after_a_switch_lands_in_the_newly_opened_chat(self, hub, backend, store):
        hub.submit("about trains")
        trains = open_id(store)
        other = hub.new_chat().chat
        backend.speak("and now a spoken one", "ok")
        hub.sync()
        assert [m.content for m in store.messages(other.id)] == ["and now a spoken one", "ok"]
        assert [m.content for m in store.messages(trains)] == ["about trains", "Done."]

    def test_restored_turns_are_not_stored_a_second_time(self, hub, backend, store):
        hub.submit("about trains")
        trains = open_id(store)
        hub.new_chat()
        hub.open_chat(trains)
        hub.sync()
        assert len(store.messages(trains)) == 2

    def test_nothing_is_opened_while_a_typed_request_is_in_flight_even_before_jarvis_reports_busy(self, hub, backend, store):
        hub.submit("about trains")
        trains = open_id(store)
        other = hub.new_chat().chat
        backend.mode = "hold"
        hub.submit("slow one")
        assert backend.busy is False
        assert hub.open_chat(trains).status == "busy"
        assert hub.new_chat().status == "busy"
        assert hub.delete_chat(other.id) == "busy"
        assert open_id(store) == other.id
        backend.release()

    def test_opening_is_refused_while_a_request_runs(self, hub, backend, store):
        hub.submit("about trains")
        trains = open_id(store)
        other = hub.new_chat().chat
        backend.busy = True
        assert hub.open_chat(trains).status == "busy"
        assert open_id(store) == other.id

    def test_a_new_chat_starts_with_an_empty_conversation(self, hub, backend, store):
        hub.submit("about trains")
        chat = hub.new_chat().chat
        assert backend.memory.all_messages() == []
        assert open_id(store) == chat.id

    def test_a_new_chat_can_start_inside_a_project(self, hub, store):
        project = store.create_project("Work")
        chat = hub.new_chat(project_id=project.id).chat
        assert store.get_chat(chat.id).project_id == project.id

    def test_a_new_chat_is_refused_while_a_request_runs(self, hub, backend, store):
        backend.busy = True
        before = len(store.list_chats(ANY_PROJECT))
        assert hub.new_chat().status == "busy"
        assert len(store.list_chats(ANY_PROJECT)) == before

    def test_an_unknown_chat_cannot_be_opened(self, hub):
        assert hub.open_chat("nope").status == "unknown"

    def test_opening_sends_the_chats_private_turns_marked_private(self, hub, backend, store):
        backend.memory.add_message("assistant", "You were in Code", diary=False)
        hub.sync()
        chat = open_id(store)
        hub.new_chat()
        hub.open_chat(chat)
        assert backend.switch_calls[-1] == [{"role": "assistant", "content": "You were in Code", "diary": False}]

    def test_only_the_latest_turns_of_a_long_chat_are_restored(self, backend, store):
        hub = ChatHub(backend, store, restore_turns=4)
        hub.sync()
        chat = open_id(store)
        for i in range(5):
            store.append_message(chat, "user", f"q{i}", ts=float(i))
            store.append_message(chat, "assistant", f"a{i}", ts=float(i) + 0.5)
        other = hub.new_chat().chat
        hub.open_chat(chat)
        assert [m["content"] for m in backend.memory.all_messages()] == ["q3", "a3", "q4", "a4"]
        assert other.id != chat


class TestDeleting:
    def test_deleting_the_open_chat_empties_the_conversation(self, hub, backend, store):
        hub.submit("about trains")
        chat = open_id(store)
        assert hub.delete_chat(chat) == "ok"
        assert store.get_chat(chat) is None and backend.memory.all_messages() == []

    def test_deleting_the_open_chat_opens_the_next_one_with_its_conversation(self, hub, backend, store):
        hub.submit("about trains")
        trains = open_id(store)
        hub.new_chat()
        hub.submit("about boats")
        boats = open_id(store)
        assert hub.delete_chat(boats) == "ok"
        assert open_id(store) == trains
        assert [m["content"] for m in backend.memory.all_messages()] == ["about trains", "Done."]
        hub.sync()
        assert len(store.messages(trains)) == 2  # the restored turns are not stored again

    def test_deleting_another_chat_leaves_the_open_one_alone(self, hub, backend, store):
        hub.submit("about trains")
        trains = open_id(store)
        hub.new_chat()
        current = open_id(store)
        assert hub.delete_chat(trains) == "ok"
        assert open_id(store) == current

    def test_deleting_the_open_chat_is_refused_while_a_request_runs(self, hub, backend, store):
        chat = open_id(store)
        backend.busy = True
        assert hub.delete_chat(chat) == "busy"
        assert store.get_chat(chat) is not None

    def test_deleting_everything_empties_chats_projects_and_the_conversation(self, hub, backend, store):
        store.create_project("Work")
        hub.submit("about trains")
        assert hub.delete_all() == "ok"
        assert store.list_projects() == [] and store.list_chats(ANY_PROJECT) == []
        assert backend.memory.all_messages() == []


class TestModels:
    def test_choosing_a_reply_mode_is_passed_to_jarvis(self, hub, backend):
        result = hub.switch_mode("claude")
        assert result.ok and backend.reply_mode["mode"] == "claude"

    def test_a_refused_reply_mode_reports_why(self, hub, backend):
        backend.refuse_mode = "not_enabled"
        result = hub.switch_mode("claude")
        assert not result.ok and result.reason == "not_enabled"

    def test_choosing_a_local_model_is_passed_to_jarvis(self, hub, backend):
        result = hub.set_local_model("qwen3.5:9b")
        assert result.ok and backend.local_model["current"] == "qwen3.5:9b"

    def test_a_refused_local_model_reports_why(self, hub, backend):
        backend.refuse_model = "busy"
        result = hub.set_local_model("qwen3.5:9b")
        assert not result.ok and result.reason == "busy"

    def test_choosing_a_cloud_model_and_effort_is_passed_to_jarvis(self, hub, backend):
        result = hub.set_cloud_model("sonnet", "high")
        assert result.ok and backend.cloud_calls == [("sonnet", "high")]

    def test_a_refused_cloud_choice_reports_why(self, hub, backend):
        backend.refuse_cloud = "effort_unsupported"
        result = hub.set_cloud_model("sonnet", "max")
        assert not result.ok and result.reason == "effort_unsupported"

    def test_the_model_list_includes_the_active_cloud_modes_models(self, hub, backend):
        backend.cloud = {"mode": "claude", "model": "sonnet", "effort": "low", "ready": True}
        backend.cloud_options = [{"id": "sonnet", "name": "Sonnet", "is_default": False, "efforts": []}]
        cloud = hub.models()["cloud"]
        assert cloud["mode"] == "claude" and cloud["models"][0]["id"] == "sonnet"

    def test_local_mode_has_no_cloud_section(self, hub):
        assert hub.models()["cloud"] is None and hub.snapshot(0)["cloud"] is None

    def test_the_snapshot_carries_the_cloud_choice(self, hub, backend):
        backend.cloud = {"mode": "codex", "model": "gpt-6-luna", "effort": "low", "ready": True}
        assert hub.snapshot(0)["cloud"]["model"] == "gpt-6-luna"

    def test_a_cloud_chat_remembers_the_model_that_answered(self, hub, backend, store):
        backend.reply_mode = {"mode": "claude", "enabled": ["local", "claude"]}
        backend.cloud = {"mode": "claude", "model": "sonnet", "effort": "low", "ready": True}
        hub.submit("hi")
        chat = store.get_chat(open_id(store))
        assert (chat.last_mode, chat.last_model) == ("claude", "sonnet")

    def test_the_page_is_told_when_the_cloud_choice_changes(self, hub, backend):
        backend.cloud = {"mode": "claude", "model": "sonnet", "effort": "low", "ready": True}
        hub.sync()
        before = hub.snapshot(0)["rev"]
        backend.cloud = {"mode": "claude", "model": "sonnet", "effort": "high", "ready": True}
        hub.sync()
        assert hub.snapshot(0)["rev"] != before

    def test_opening_a_chat_never_changes_the_model_or_mode(self, hub, backend, store):
        backend.reply_mode = {"mode": "claude", "enabled": ["local", "claude"]}
        hub.submit("secret plans")
        chat = open_id(store)
        backend.reply_mode = {"mode": "local", "enabled": ["local", "claude"]}
        hub.new_chat()
        hub.open_chat(chat)
        assert backend.reply_mode["mode"] == "local"
        snapshot = hub.snapshot(0)
        assert snapshot["chat"]["last_mode"] == "claude"


class TestSnapshotAndPolling:
    def test_the_snapshot_describes_the_open_chat_and_its_messages(self, hub, backend, store):
        hub.submit("hello")
        snap = hub.snapshot(0)
        assert snap["active_chat_id"] == open_id(store)
        assert [m["text"] for m in snap["messages"]] == ["hello", "Done."]
        assert snap["ready"] is True and snap["mode"]["mode"] == "local"
        assert snap["model"]["current"] == "gemma4:12b"

    def test_only_messages_after_an_id_are_returned(self, hub, store):
        hub.submit("hello")
        first = store.messages(open_id(store))[0].id
        assert [m["text"] for m in hub.snapshot(first)["messages"]] == ["Done."]

    def test_private_flags_and_internal_ids_do_not_leak_into_messages(self, hub, backend):
        hub.submit("hello")
        message = hub.snapshot(0)["messages"][0]
        assert set(message) == {"id", "role", "text", "ts", "source"}

    def test_a_wait_returns_when_something_changes(self, hub, backend):
        rev = hub.snapshot(0)["rev"]
        threading.Timer(0.1, lambda: (backend.speak("hi", "hello"), hub.sync())).start()
        started = time.monotonic()
        snap = hub.wait(rev=rev, after=0, timeout_sec=5)
        assert time.monotonic() - started < 4 and snap["rev"] != rev

    def test_a_wait_gives_up_after_the_timeout(self, hub):
        rev = hub.snapshot(0)["rev"]
        started = time.monotonic()
        snap = hub.wait(rev=rev, after=0, timeout_sec=0.2)
        assert 0.15 <= time.monotonic() - started < 2 and snap["rev"] == rev

    def test_the_page_is_told_when_the_chat_list_changes(self, hub, store):
        before = hub.snapshot(0)["library_rev"]
        hub.submit("hello")
        assert hub.snapshot(0)["library_rev"] != before

    def test_the_assistant_state_reads_thinking_while_a_typed_request_runs(self, hub, backend):
        backend.mode = "hold"
        hub.submit("slow")
        assert hub.snapshot(0)["state"] == "thinking"
        backend.release()
        assert hub.snapshot(0)["state"] == "idle"

    def test_an_unready_daemon_is_reported(self, hub, backend):
        backend.memory = None
        assert hub.snapshot(0)["ready"] is False

    def test_the_library_lists_projects_and_chats(self, hub, store):
        project = store.create_project("Work")
        chat = hub.new_chat(project_id=project.id).chat
        library = hub.library()
        assert [p["name"] for p in library["projects"]] == ["Work"]
        assert chat.id in {c["id"] for c in library["chats"]}
        assert library["active_chat_id"] == chat.id


class TestBackgroundSync:
    def test_a_started_hub_mirrors_voice_without_being_asked(self, backend, store):
        h = ChatHub(backend, store, sync_interval_sec=0.05)
        h.start()
        try:
            backend.speak("good morning", "Good morning")
            deadline = time.time() + 5
            while time.time() < deadline and not store.messages(open_id(store)):
                time.sleep(0.02)
            assert [m.content for m in store.messages(open_id(store))] == ["good morning", "Good morning"]
        finally:
            h.stop()
