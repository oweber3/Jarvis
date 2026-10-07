"""Web chat storage: projects, chats and messages in the Jarvis database file.

Behaviour only: what the owner sees when creating, grouping, renaming, moving and deleting chats.
See ``webchat/webchat.spec.md``.
"""
import sqlite3

import pytest

from jarvis.memory.chat_store import ANY_PROJECT, TITLE_MAX_CHARS, UNFILED, ChatStore


@pytest.fixture
def store(tmp_path):
    s = ChatStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


def titles(chats):
    return [c.title for c in chats]


class TestProjects:
    def test_created_projects_are_listed_in_creation_order(self, store):
        store.create_project("Homework")
        store.create_project("Garden")
        assert [p.name for p in store.list_projects()] == ["Homework", "Garden"]

    def test_names_are_trimmed_and_must_not_be_empty(self, store):
        assert store.create_project("  Home  ").name == "Home"
        with pytest.raises(ValueError):
            store.create_project("   ")

    def test_rename(self, store):
        project = store.create_project("Old")
        assert store.rename_project(project.id, "New") is True
        assert store.list_projects()[0].name == "New"

    def test_renaming_or_deleting_an_unknown_project_reports_false(self, store):
        assert store.rename_project("nope", "x") is False
        assert store.delete_project("nope") is False

    def test_deleting_a_project_keeps_its_chats_as_unfiled(self, store):
        project = store.create_project("Work")
        chat = store.create_chat(project_id=project.id)
        assert store.delete_project(project.id) is True
        assert store.get_chat(chat.id).project_id is None
        assert store.list_projects() == []


class TestChats:
    def test_a_new_chat_is_unfiled_and_untitled(self, store):
        chat = store.create_chat()
        assert chat.project_id is None and chat.title == ""
        assert store.get_chat(chat.id) == chat

    def test_creating_a_chat_in_an_unknown_project_is_refused(self, store):
        with pytest.raises(ValueError):
            store.create_chat(project_id="nope")

    def test_lists_per_project_unfiled_and_all_newest_first(self, store):
        work = store.create_project("Work")
        first = store.create_chat(project_id=work.id, title="a")
        store.create_chat(title="b")
        third = store.create_chat(project_id=work.id, title="c")
        assert titles(store.list_chats(work.id)) == ["c", "a"]
        assert titles(store.list_chats(UNFILED)) == ["b"]
        assert sorted(titles(store.list_chats(ANY_PROJECT))) == ["a", "b", "c"]
        assert {first.id, third.id} == {c.id for c in store.list_chats(work.id)}

    def test_activity_moves_a_chat_to_the_top(self, store):
        old = store.create_chat(title="old")
        store.create_chat(title="newer")
        store.append_message(old.id, "user", "hello", ts=2_000_000_000.0)
        assert store.list_chats(ANY_PROJECT)[0].id == old.id

    def test_rename_and_move(self, store):
        project = store.create_project("Work")
        chat = store.create_chat()
        assert store.rename_chat(chat.id, "  Plans  ") is True
        assert store.move_chat(chat.id, project.id) is True
        moved = store.get_chat(chat.id)
        assert (moved.title, moved.project_id) == ("Plans", project.id)
        assert store.move_chat(chat.id, None) is True
        assert store.get_chat(chat.id).project_id is None

    def test_moving_into_an_unknown_project_changes_nothing(self, store):
        chat = store.create_chat()
        assert store.move_chat(chat.id, "nope") is False
        assert store.get_chat(chat.id).project_id is None

    def test_delete_removes_the_chat_and_its_messages(self, store, tmp_path):
        chat = store.create_chat()
        store.append_message(chat.id, "user", "secret plans", ts=1.0)
        assert store.delete_chat(chat.id) is True
        assert store.get_chat(chat.id) is None
        assert store.messages(chat.id) == []
        with sqlite3.connect(str(tmp_path / "jarvis.db")) as raw:
            assert raw.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0

    def test_remembers_the_mode_and_model_a_chat_last_used(self, store):
        chat = store.create_chat()
        store.set_last_model(chat.id, "claude", "")
        store.set_last_model(chat.id, "local", "gemma4:12b")
        found = store.get_chat(chat.id)
        assert (found.last_mode, found.last_model) == ("local", "gemma4:12b")


class TestMessages:
    def test_messages_come_back_in_order(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "user", "hi", ts=1.0)
        store.append_message(chat.id, "assistant", "hello", ts=2.0, source="voice")
        got = store.messages(chat.id)
        assert [(m.role, m.content, m.source) for m in got] == [
            ("user", "hi", "typed"), ("assistant", "hello", "voice")]

    def test_only_messages_after_an_id_can_be_asked_for(self, store):
        chat = store.create_chat()
        first = store.append_message(chat.id, "user", "one", ts=1.0)
        store.append_message(chat.id, "assistant", "two", ts=2.0)
        assert [m.content for m in store.messages(chat.id, after_id=first.id)] == ["two"]

    def test_last_messages_keeps_the_newest_in_order(self, store):
        chat = store.create_chat()
        for i in range(5):
            store.append_message(chat.id, "user", f"m{i}", ts=float(i))
        assert [m.content for m in store.last_messages(chat.id, 3)] == ["m2", "m3", "m4"]

    def test_private_turns_stay_marked_private(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "assistant", "you used Code for 2h", ts=1.0, private=True)
        store.append_message(chat.id, "user", "hi", ts=2.0)
        assert [m.private for m in store.messages(chat.id)] == [True, False]

    def test_unknown_roles_and_chats_are_refused(self, store):
        chat = store.create_chat()
        with pytest.raises(ValueError):
            store.append_message(chat.id, "system", "x", ts=1.0)
        with pytest.raises(ValueError):
            store.append_message("nope", "user", "x", ts=1.0)

    def test_the_first_user_message_titles_an_untitled_chat(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "user", "  What   is\nthe weather like today?  ", ts=1.0)
        store.append_message(chat.id, "user", "and tomorrow?", ts=2.0)
        assert store.get_chat(chat.id).title == "What is the weather like today?"

    def test_a_long_first_message_gives_a_bounded_title(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "user", "x" * 500, ts=1.0)
        assert len(store.get_chat(chat.id).title) <= TITLE_MAX_CHARS

    def test_an_owner_chosen_title_is_not_overwritten(self, store):
        chat = store.create_chat(title="Mine")
        store.append_message(chat.id, "user", "hello", ts=1.0)
        assert store.get_chat(chat.id).title == "Mine"

    def test_a_reply_alone_does_not_title_a_chat(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "assistant", "Good morning", ts=1.0)
        assert store.get_chat(chat.id).title == ""


class TestState:
    def test_the_open_chat_is_remembered_across_restarts(self, tmp_path):
        path = str(tmp_path / "jarvis.db")
        first = ChatStore(path)
        chat = first.create_chat()
        first.set_active_chat_id(chat.id)
        first.close()
        second = ChatStore(path)
        try:
            assert second.get_active_chat_id() == chat.id
            assert second.get_chat(chat.id) is not None
        finally:
            second.close()

    def test_a_deleted_chat_is_no_longer_the_open_one(self, store):
        chat = store.create_chat()
        store.set_active_chat_id(chat.id)
        store.delete_chat(chat.id)
        assert store.get_active_chat_id() is None

    def test_delete_all_clears_every_project_chat_and_message(self, store):
        project = store.create_project("Work")
        chat = store.create_chat(project_id=project.id)
        store.append_message(chat.id, "user", "hi", ts=1.0)
        store.set_active_chat_id(chat.id)
        store.delete_all()
        assert store.list_projects() == [] and store.list_chats(ANY_PROJECT) == []
        assert store.get_active_chat_id() is None


class TestSharedDatabaseFile:
    def test_works_beside_the_main_database_without_touching_its_tables(self, tmp_path):
        from jarvis.memory.db import Database
        path = str(tmp_path / "jarvis.db")
        db = Database(path, sqlite_vss_path=None)
        store = ChatStore(path)
        try:
            chat = store.create_chat(title="x")
            store.append_message(chat.id, "user", "hi", ts=1.0)
            assert db.get_all_conversation_summaries() == []
        finally:
            store.close()
            db.close()
