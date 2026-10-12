"""The memory viewer shows the user's lists and notes, read only (memory/lists_notes.spec.md)."""
from __future__ import annotations

import pytest

flask = pytest.importorskip("flask")

from jarvis.memory.lists_notes import ListsNotesStore  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    from desktop_app import memory_viewer
    db_path = str(tmp_path / "jarvis.db")
    monkeypatch.setattr(memory_viewer, "_get_db_path", lambda: db_path)
    memory_viewer.app.config["TESTING"] = True
    return memory_viewer.app.test_client(), db_path


@pytest.mark.unit
def test_lists_with_their_items_and_newest_notes_first(client):
    test_client, db_path = client
    store = ListsNotesStore(db_path)
    store.add_items("shopping", ["milk", "eggs"])
    store.check_items("shopping", ["eggs"])
    store.add_note("first note")
    store.add_note("second note")
    store.close()

    data = test_client.get("/api/lists_notes").get_json()

    assert data["lists"] == [{"list": "shopping", "items": [{"text": "milk", "done": False},
                                                            {"text": "eggs", "done": True}]}]
    assert [n["text"] for n in data["notes"]] == ["second note", "first note"]


@pytest.mark.unit
def test_empty_when_nothing_was_saved(client):
    test_client, _ = client
    assert test_client.get("/api/lists_notes").get_json() == {"lists": [], "notes": []}


@pytest.mark.unit
def test_the_page_has_a_lists_and_notes_tab(client):
    test_client, _ = client
    page = test_client.get("/").get_data(as_text=True)
    assert 'data-tab="lists"' in page
    assert "/api/lists_notes" in page
