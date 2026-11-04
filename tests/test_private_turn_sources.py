"""Private turns remember which opt-in source made them private.

A turn that quoted the activity log, the user's documents, a calendar or email is private. It records
which of those sources it quoted, in memory and in the web chat's rows in the Jarvis database, so that a
private turn reaches Codex or Claude only while every source it quoted is still turned on and shared
with the cloud. Rows written before sources were recorded count as "unknown" and are never shared.
See ``bridge/bridge.spec.md`` (shared dialogue) and ``documents/documents.spec.md``.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from fake_webchat_backend import FakeWebChatBackend
from jarvis.bridge.adapter import build_dialogue_context
from jarvis.bridge.settings import bridge_settings
from jarvis.memory import activity_log as al
from jarvis.memory.chat_store import ChatStore
from jarvis.memory.conversation import DialogueMemory
from jarvis.webchat.hub import ChatHub

MODES = ("codex", "claude")
# Every opt-in source on and shared with the cloud.
ALL_SHARED = dict(activity_log_enabled=True, activity_log_share_with_cloud=True,
                  document_search_enabled=True, document_search_share_with_cloud=True,
                  calendar_enabled=True, calendar_share_with_cloud=True,
                  email_enabled=True, email_share_with_cloud=True)


def cfg(**overrides):
    return SimpleNamespace(**{**ALL_SHARED, **overrides})


def shared_contents(memory, config):
    return {mode: [m["content"] for m in build_dialogue_context(memory, bridge_settings(config, mode))]
            for mode in MODES}


def memory_with(*turns):
    """``turns``: (content, sources) pairs; ``None`` sources for a turn that is not private."""
    dm = DialogueMemory(inactivity_timeout=300)
    for i, (content, sources) in enumerate(turns):
        role = "user" if i % 2 == 0 else "assistant"
        if sources is None:
            dm.add_message(role, content)
        else:
            dm.add_message(role, content, diary=False, private_sources=sources)
    return dm


@pytest.fixture
def store(tmp_path):
    s = ChatStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def clean_turn_flag():
    al.consume_turn_private()
    yield
    al.consume_turn_private()


class TestTheTurnFlagRecordsSources:
    def test_a_marked_turn_reports_the_sources_it_quoted(self):
        al.mark_turn_private("calendar")
        al.mark_turn_private("email")
        assert al.consume_turn_private() == {"calendar", "email"}
        assert not al.consume_turn_private()

    def test_a_mark_without_a_source_counts_as_unknown(self):
        al.mark_turn_private()
        assert al.consume_turn_private() == {al.UNKNOWN_SOURCE}


class TestSharingWithTheCloud:
    def test_a_turn_from_an_enabled_shared_source_is_shared(self):
        dm = memory_with(("what is on my calendar", {"calendar"}), ("Lunch at one", {"calendar"}))
        for mode, shared in shared_contents(dm, cfg()).items():
            assert shared == ["what is on my calendar", "Lunch at one"], mode

    def test_a_turn_whose_source_was_turned_off_is_not_shared(self):
        dm = memory_with(("find my essay", {"documentSearch"}), ("It says...", {"documentSearch"}), ("thanks", None))
        for mode, shared in shared_contents(dm, cfg(document_search_enabled=False)).items():
            assert shared == ["thanks"], mode

    def test_a_turn_whose_source_is_not_shared_with_the_cloud_is_not_shared(self):
        dm = memory_with(("what did I work on", {"activityLog"}), ("Code, mostly", {"activityLog"}))
        for mode, shared in shared_contents(dm, cfg(activity_log_share_with_cloud=False)).items():
            assert shared == [], mode

    def test_every_recorded_source_must_be_shared(self):
        dm = memory_with(("mail and calendar", {"calendar", "email"}))
        assert all(s == [] for s in shared_contents(dm, cfg(email_share_with_cloud=False)).values())
        assert all(s == ["mail and calendar"] for s in shared_contents(dm, cfg()).values())

    def test_an_unshared_source_does_not_hold_back_turns_from_other_sources(self):
        dm = memory_with(("what is on my calendar", {"calendar"}))
        config = cfg(document_search_share_with_cloud=False, email_enabled=False)
        assert all(s == ["what is on my calendar"] for s in shared_contents(dm, config).values())

    def test_a_turn_of_unknown_source_is_never_shared(self):
        dm = memory_with(("old private turn", {al.UNKNOWN_SOURCE}), ("ok", None))
        assert all(s == ["ok"] for s in shared_contents(dm, cfg()).values())

    def test_a_private_turn_with_no_recorded_source_counts_as_unknown(self):
        dm = DialogueMemory(inactivity_timeout=300)
        dm.add_message("user", "private, source not given", diary=False)
        assert all(s == [] for s in shared_contents(dm, cfg()).values())

    def test_turns_that_are_not_private_are_shared_whatever_the_switches_say(self):
        dm = memory_with(("hello", None), ("Hi", None))
        nothing_shared = SimpleNamespace()
        assert all(s == ["hello", "Hi"] for s in shared_contents(dm, nothing_shared).values())

    def test_a_cloud_reply_built_on_a_shared_private_turn_carries_its_sources(self, monkeypatch):
        from jarvis.bridge import adapter, runtime
        from jarvis.bridge.tools import BridgeOutcome
        dm = memory_with(("what is on my calendar", {"calendar"}))
        service = SimpleNamespace(mode="claude", accepts_images=False,
                                  run_request=lambda *a, **k: BridgeOutcome(kind="reply", text="Lunch at one"))
        monkeypatch.setattr(runtime, "get_service", lambda: service)
        seen = []
        adapter.run_bridge_reply("claude", None, cfg(), None, "and tomorrow?", dm, "en", True,
                                 lambda *a, **k: seen.append(al.consume_turn_private()) or "")
        assert seen == [{"calendar"}]


class TestTheWebChatDatabase:
    def test_a_private_message_is_stored_with_its_sources(self, store):
        chat = store.create_chat()
        store.append_message(chat.id, "user", "my mail", ts=1.0, private=True, private_sources={"email"})
        store.append_message(chat.id, "assistant", "Two new", ts=2.0, private=True,
                             private_sources={"email", "calendar"})
        store.append_message(chat.id, "user", "thanks", ts=3.0)
        assert [set(m.private_sources) for m in store.messages(chat.id)] == [{"email"}, {"calendar", "email"}, set()]

    def test_rows_written_before_sources_were_recorded_are_of_unknown_source(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE chats (id TEXT PRIMARY KEY, project_id TEXT, title TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL, updated_at REAL NOT NULL, last_mode TEXT NOT NULL DEFAULT '',
              last_model TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
              role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL, source TEXT NOT NULL DEFAULT 'typed',
              private INTEGER NOT NULL DEFAULT 0, steps TEXT NOT NULL DEFAULT '',
              attachments TEXT NOT NULL DEFAULT '');
            INSERT INTO chats VALUES ('c1', NULL, 'Old', 1, 1, '', '', 0);
            INSERT INTO chat_messages(chat_id, role, content, ts, private) VALUES ('c1', 'user', 'activity?', 1, 1);
            INSERT INTO chat_messages(chat_id, role, content, ts, private) VALUES ('c1', 'assistant', 'hi', 2, 0);
        """)
        conn.commit()
        conn.close()
        upgraded = ChatStore(path)
        try:
            assert [(m.private, m.private_sources) for m in upgraded.messages("c1")] == [
                (True, (al.UNKNOWN_SOURCE,)), (False, ())]
        finally:
            upgraded.close()

    def test_a_reopened_chat_shares_only_the_private_turns_whose_sources_are_still_shared(self, store):
        backend = FakeWebChatBackend()
        hub = ChatHub(backend, store, sync_interval_sec=0.05)
        try:
            hub.sync()
            backend.memory.add_message("user", "what is on my calendar", diary=False, private_sources={"calendar"})
            backend.memory.add_message("assistant", "Lunch at one", diary=False, private_sources={"calendar"})
            backend.memory.add_message("user", "find my essay", diary=False, private_sources={"documentSearch"})
            backend.memory.add_message("assistant", "It says...", diary=False, private_sources={"documentSearch"})
            hub.sync()
            chat_id = store.get_active_chat_id()
            hub.new_chat()
            backend.memory = DialogueMemory()  # Jarvis restarted with an empty memory
            assert hub.open_chat(chat_id).status == "ok"
            config = cfg(document_search_enabled=False)
            for mode, shared in shared_contents(backend.memory, config).items():
                assert shared == ["what is on my calendar", "Lunch at one"], mode
        finally:
            hub.stop()
