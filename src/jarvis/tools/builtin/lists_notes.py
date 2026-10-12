"""``listControl`` and ``noteControl``: the user's named lists and quick notes, stored on this PC.

Thin tools over ``memory.lists_notes.ListsNotesStore``. They return raw JSON; the reply model writes
the answer. Clearing or deleting a whole list and deleting a note confirm by voice; single-item
changes do not. Both are personal-data tools, offered to Codex and Claude reply modes only when that
mode shares long-term memory. See ``memory/lists_notes.spec.md``.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...debug import debug_log
from ...memory.lists_notes import ListsNotesStore, SEARCH_DEFAULT_LIMIT
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

_LIST_ACTIONS = ["add", "remove", "check", "show", "lists", "clear", "delete"]
_LIST_READS = frozenset({"show", "lists"})
_LIST_CONFIRMED = frozenset({"clear", "delete"})
_NOTE_ACTIONS = ["add", "search", "recent", "delete"]
_NOTE_READS = frozenset({"search", "recent"})
_PREVIEW_CHARS = 60


def _fail(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, reply_text=None, error_message=message)


def _ok(payload: Dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(success=True, reply_text=json.dumps(payload, ensure_ascii=False))


def _items_arg(args: Dict[str, Any]) -> List[str]:
    items = args.get("items")
    if isinstance(items, str):
        return [items]
    if isinstance(items, list):
        return [str(item) for item in items if item is not None]
    return []


def _note_id(args: Dict[str, Any]) -> Optional[int]:
    value = args.get("id")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1  # names no note, so it fails rather than deleting the newest


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _PREVIEW_CHARS else flat[:_PREVIEW_CHARS].rstrip() + "..."


class ListControlTool(Tool):
    def returns_outside_content_for(self, args: Optional[Dict[str, Any]]) -> bool:
        # An item may have been copied from a web page (lists_notes.spec.md, Privacy).
        return isinstance(args, dict) and args.get("action") in _LIST_READS

    @property
    def name(self) -> str:
        return "listControl"

    @property
    def description(self) -> str:
        # The router reads only the first 120 characters.
        return ("The user's named lists (shopping, to-do, any name): add, remove or check off items, read a list "
                "back, list all lists, clear or delete a list. Stored on this PC. NOT for free-form notes "
                "(use noteControl) or files (use localFiles).")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _LIST_ACTIONS,
                           "description": "add puts items on a list (creating it); remove takes items off; "
                                          "check marks items done; show reads a list; lists names every list; "
                                          "clear empties a list; delete removes a whole list."},
                "list": {"type": "string",
                         "description": "The list's name as the user says it, without the word list, for "
                                        "example shopping or to-do. Not needed for lists."},
                "items": {"type": "array", "items": {"type": "string"},
                          "description": "For add, remove and check: one entry per item, for example "
                                         "[\"milk\", \"eggs\"]."},
            },
            "required": ["action"],
        }

    def classify_safety(self, args, cfg):
        from ..confirmation import ConfirmationRequest, SafetyTier
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
        request = ConfirmationRequest(tool_name=self.name, tier=SafetyTier.SAFE, action=str(action or self.name),
                                      target="", parameters=dict(args))
        if action not in _LIST_CONFIRMED:
            return request
        store = ListsNotesStore(cfg.db_path)
        try:
            name = store.resolve_name(str(args.get("list") or ""))
        finally:
            store.close()
        if name is None:
            return request  # nothing to clear or delete; running says which lists exist
        request.tier = SafetyTier.CONFIRM_VOICE
        request.action = f"{action} the list {name}"
        request.consequence = ("Every item on the list will be removed." if action == "clear"
                               else "The list and its items will be deleted.")
        return request

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        if action not in _LIST_ACTIONS:
            return _fail(f"Use one of these actions: {', '.join(_LIST_ACTIONS)}.")
        debug_log(f"listControl {action}", "lists")
        store = ListsNotesStore(context.cfg.db_path)
        try:
            if action == "lists":
                found = store.lists()
                context.user_print(f"📋 {len(found)} lists")
                return _ok({"lists": found})
            name = str(args.get("list") or "").strip()
            if not name:
                return _fail("Say which list, for example shopping.")
            if action == "add":
                try:
                    result = store.add_items(name, _items_arg(args))
                except ValueError:
                    return _fail("Say which items to add.")
                context.user_print(f"📝 List updated: {len(result['added']) + len(result['reopened'])} added")
                return _ok(result)
            if action in ("remove", "check"):
                change = store.remove_items if action == "remove" else store.check_items
                try:
                    result = change(name, _items_arg(args))
                except ValueError:
                    return _fail(f"Say which items to {action}.")
            elif action == "show":
                result = store.show(name)
            elif action == "clear":
                result = store.clear(name)
            else:
                result = store.delete_list(name)
            if result is None:
                existing = [entry["name"] for entry in store.lists()]
                context.user_print("⚠️ No such list")
                return _fail("There is no list with that name. Existing lists: "
                             + (", ".join(existing) if existing else "none") + ".")
            context.user_print(f"📋 List {action} done")
            return _ok(result)
        finally:
            store.close()


class NoteControlTool(Tool):
    def returns_outside_content_for(self, args: Optional[Dict[str, Any]]) -> bool:
        # A note may have been copied from a web page (lists_notes.spec.md, Privacy).
        return isinstance(args, dict) and args.get("action") in _NOTE_READS

    @property
    def name(self) -> str:
        return "noteControl"

    @property
    def description(self) -> str:
        # The router reads only the first 120 characters.
        return ("The user's notes: save a note (note that..., take a note, write down), find notes about "
                "something, read the last note, delete a note. Stored on this PC. Nothing is saved as a note unless "
                "this tool is called. NOT for items on a named list (use listControl) or files (use localFiles).")

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": _NOTE_ACTIONS,
                           "description": "add saves a note; search finds notes about something; recent reads "
                                          "the newest notes; delete removes a note."},
                "text": {"type": "string", "description": "For add: the note, in the user's words."},
                "query": {"type": "string", "description": "For search: what the notes are about, a few "
                                                           "key words."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "OPTIONAL. For search and recent: most notes to return."},
                "id": {"type": "integer",
                       "description": "OPTIONAL. For delete: the note's id from search or recent. Without it "
                                      "the newest note is deleted."},
            },
            "required": ["action"],
        }

    def classify_safety(self, args, cfg):
        from ..confirmation import ConfirmationRequest, SafetyTier
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
        request = ConfirmationRequest(tool_name=self.name, tier=SafetyTier.SAFE, action=str(action or self.name),
                                      target="", parameters=dict(args))
        if action != "delete":
            return request
        store = ListsNotesStore(cfg.db_path)
        try:
            note = store.get_note(_note_id(args))
        finally:
            store.close()
        if note is None:
            return request  # nothing to delete; running says so
        request.tier = SafetyTier.CONFIRM_VOICE
        request.action = f"delete the note \"{_preview(note['text'])}\""
        request.consequence = "The note will be deleted."
        return request

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        if action not in _NOTE_ACTIONS:
            return _fail(f"Use one of these actions: {', '.join(_NOTE_ACTIONS)}.")
        debug_log(f"noteControl {action}", "notes")
        try:
            limit = int(args.get("limit")) if args.get("limit") is not None else None
        except (TypeError, ValueError):
            limit = None
        store = ListsNotesStore(context.cfg.db_path)
        try:
            if action == "add":
                try:
                    note = store.add_note(args.get("text"))
                except ValueError:
                    return _fail("Say what the note should say.")
                context.user_print("📝 Note saved")
                return _ok({"saved": note})
            if action == "search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return _fail("Say what the notes are about.")
                notes = store.search_notes(query, limit or SEARCH_DEFAULT_LIMIT)
                context.user_print(f"🔎 {len(notes)} notes found")
                return _ok({"notes": notes})
            if action == "recent":
                notes = store.recent_notes(limit or 1)
                context.user_print(f"📒 {len(notes)} notes")
                return _ok({"notes": notes})
            note = store.delete_note(_note_id(args))
            if note is None:
                return _fail("There is no such note.")
            context.user_print("🗑️ Note deleted")
            return _ok({"deleted": note})
        finally:
            store.close()
