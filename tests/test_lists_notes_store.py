"""Lists and notes storage in the Jarvis database (memory/lists_notes.spec.md)."""
from __future__ import annotations

import sqlite3

import pytest

from jarvis.memory.lists_notes import ListsNotesStore


@pytest.fixture
def store(tmp_path):
    s = ListsNotesStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


def texts(items):
    return [item["text"] for item in items]


@pytest.mark.unit
class TestLists:
    def test_adding_creates_the_list_and_keeps_the_items_in_order(self, store):
        result = store.add_items("Shopping", ["milk", "eggs"])
        assert result["list"] == "Shopping"
        assert result["added"] == ["milk", "eggs"]
        assert texts(store.show("shopping")["items"]) == ["milk", "eggs"]

    def test_an_item_already_on_the_list_is_kept_once(self, store):
        store.add_items("shopping", ["Milk"])
        result = store.add_items("shopping", ["milk", "bread"])
        assert result["already_there"] == ["milk"]
        assert texts(store.show("shopping")["items"]) == ["Milk", "bread"]

    def test_names_differing_only_in_case_and_punctuation_are_one_list(self, store):
        store.add_items("To-do", ["call the bank"])
        store.add_items("to do", ["book dentist"])
        store.add_items("TODO", ["pay rent"])
        assert len(store.lists()) == 1
        assert texts(store.show("todo")["items"]) == ["call the bank", "book dentist", "pay rent"]

    def test_a_name_with_extra_trailing_words_finds_the_existing_list(self, store):
        store.add_items("shopping", ["milk"])
        store.add_items("shopping list", ["eggs"])
        assert [entry["name"] for entry in store.lists()] == ["shopping"]
        assert texts(store.show("shopping list")["items"]) == ["milk", "eggs"]

    def test_a_shorter_name_finds_a_longer_list(self, store):
        store.add_items("shopping list", ["milk"])
        assert texts(store.show("shopping")["items"]) == ["milk"]

    def test_an_ambiguous_short_name_resolves_to_nothing(self, store):
        store.add_items("work tasks", ["a"])
        store.add_items("work ideas", ["b"])
        assert store.show("work") is None

    def test_works_with_any_language(self, store):
        store.add_items("Einkaufsliste", ["Milch", "Äpfel"])
        store.check_items("einkaufsliste", ["äpfel"])
        items = store.show("EINKAUFSLISTE")["items"]
        assert [(i["text"], i["done"]) for i in items] == [("Milch", False), ("Äpfel", True)]

    def test_removing_reports_what_was_not_on_the_list(self, store):
        store.add_items("shopping", ["milk", "eggs"])
        result = store.remove_items("shopping", ["milk", "caviar"])
        assert result["removed"] == ["milk"]
        assert result["not_found"] == ["caviar"]
        assert texts(store.show("shopping")["items"]) == ["eggs"]

    def test_checking_off_marks_items_done_and_adding_again_reopens_them(self, store):
        store.add_items("shopping", ["milk", "eggs"])
        assert store.check_items("shopping", ["eggs"])["checked"] == ["eggs"]
        assert [i["done"] for i in store.show("shopping")["items"]] == [False, True]
        assert store.add_items("shopping", ["eggs"])["reopened"] == ["eggs"]
        assert [i["done"] for i in store.show("shopping")["items"]] == [False, False]

    def test_changing_a_list_that_does_not_exist_returns_none(self, store):
        assert store.remove_items("nothing", ["x"]) is None
        assert store.check_items("nothing", ["x"]) is None
        assert store.clear("nothing") is None
        assert store.delete_list("nothing") is None

    def test_lists_reports_open_and_done_counts(self, store):
        store.add_items("shopping", ["milk", "eggs", "bread"])
        store.check_items("shopping", ["eggs"])
        store.add_items("to-do", ["call mum"])
        assert store.lists() == [
            {"name": "shopping", "open": 2, "done": 1},
            {"name": "to-do", "open": 1, "done": 0},
        ]

    def test_clearing_empties_the_list_but_keeps_it(self, store):
        store.add_items("shopping", ["milk", "eggs"])
        assert store.clear("shopping")["removed_count"] == 2
        assert store.show("shopping")["items"] == []
        assert [entry["name"] for entry in store.lists()] == ["shopping"]

    def test_deleting_removes_the_list_and_its_items(self, store, tmp_path):
        store.add_items("shopping", ["milk"])
        assert store.delete_list("shopping")["list"] == "shopping"
        assert store.lists() == []
        conn = sqlite3.connect(str(tmp_path / "jarvis.db"))
        assert conn.execute("SELECT COUNT(*) FROM user_list_items").fetchone()[0] == 0
        conn.close()

    def test_blank_items_and_names_are_rejected(self, store):
        with pytest.raises(ValueError):
            store.add_items("  ", ["milk"])
        with pytest.raises(ValueError):
            store.add_items("shopping", ["  ", ""])

    def test_data_survives_reopening_the_database(self, tmp_path):
        path = str(tmp_path / "jarvis.db")
        first = ListsNotesStore(path)
        first.add_items("shopping", ["milk"])
        first.add_note("the wifi password is on the router")
        first.close()
        second = ListsNotesStore(path)
        assert texts(second.show("shopping")["items"]) == ["milk"]
        assert second.recent_notes(1)[0]["text"] == "the wifi password is on the router"
        second.close()


@pytest.mark.unit
class TestNotes:
    def test_recent_returns_the_newest_note_first(self, store):
        store.add_note("first")
        store.add_note("second")
        assert [n["text"] for n in store.recent_notes(2)] == ["second", "first"]
        assert [n["text"] for n in store.recent_notes(1)] == ["second"]

    def test_search_finds_notes_by_any_word_ignoring_case(self, store):
        store.add_note("The WiFi password is on the router")
        store.add_note("Dentist on Tuesday at 3")
        found = store.search_notes("wifi")
        assert [n["text"] for n in found] == ["The WiFi password is on the router"]

    def test_search_ranks_notes_with_more_matching_words_first(self, store):
        store.add_note("router is in the cupboard")
        store.add_note("wifi password is on the router")
        store.add_note("unrelated")
        found = store.search_notes("wifi router password")
        assert [n["text"] for n in found] == ["wifi password is on the router", "router is in the cupboard"]

    def test_search_without_a_match_returns_nothing(self, store):
        store.add_note("dentist on tuesday")
        assert store.search_notes("passport") == []

    def test_search_works_with_non_latin_text(self, store):
        store.add_note("Пароль от wifi на роутере")
        assert len(store.search_notes("роутере")) == 1

    def test_notes_carry_an_id_and_a_creation_time(self, store):
        note = store.add_note("hello")
        listed = store.recent_notes(1)[0]
        assert listed["id"] == note["id"]
        assert listed["created"]

    def test_deleting_a_note_by_id_or_the_newest(self, store):
        first = store.add_note("first")
        store.add_note("second")
        assert store.delete_note(first["id"])["text"] == "first"
        assert store.delete_note(None)["text"] == "second"
        assert store.recent_notes(5) == []
        assert store.delete_note(None) is None
        assert store.delete_note(12345) is None

    def test_get_note_and_newest_note(self, store):
        assert store.get_note(None) is None
        note = store.add_note("only")
        assert store.get_note(note["id"])["text"] == "only"
        assert store.get_note(None)["text"] == "only"

    def test_blank_notes_are_rejected(self, store):
        with pytest.raises(ValueError):
            store.add_note("   ")
