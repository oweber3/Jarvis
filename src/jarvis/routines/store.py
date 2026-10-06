"""The live set of routines, and writing changes to them in ``config.json``.

The live set is what the configuration held at start, replaced after every change by a fresh load of
the file, so a change takes effect on the next request without a restart, for the fast path, the tool
and the model alike. A write re-reads the ``routines`` value from the file and changes only the named
routine; every other key, and routines that failed the structural check, are kept untouched. See
``routines.spec.md``, Creating and changing routines.
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Optional

from ..debug import debug_log
from ..utils import names
from .definitions import Routine, load_routines

_lock = threading.RLock()
_live: Optional[dict[str, Routine]] = None
# The configuration value last parsed for ``current`` and its routines, so unchanged settings are not
# parsed (and logged) again on every request.
_parsed_source: Any = None
_parsed: dict[str, Routine] = {}


class RoutineStoreError(Exception):
    """A change could not be written; nothing was changed."""


def current(cfg=None) -> dict[str, Routine]:
    """The routines that run now: the last loaded set, or the ones in the settings Jarvis started with."""
    global _parsed_source, _parsed
    with _lock:
        if _live is not None:
            return _live
        value = getattr(cfg, "routines", None) if cfg is not None else None
        if value != _parsed_source:
            _parsed_source, _parsed = copy.deepcopy(value), load_routines(value)
        return _parsed


def load(value) -> dict[str, Routine]:
    """Replace the live set with the routines in a ``routines`` configuration value."""
    global _live
    loaded = load_routines(value)
    with _lock:
        _live = loaded
    return loaded


def reload() -> dict[str, Routine]:
    """Replace the live set with the routines in ``config.json`` as it is now."""
    from ..config import read_config_file
    return load(read_config_file().get("routines"))


def reset() -> None:
    """Forget the live set; ``current`` then reads the settings again."""
    global _live, _parsed_source, _parsed
    with _lock:
        _live, _parsed_source, _parsed = None, None, {}


def _write(change, outcome: str) -> dict[str, Routine]:
    from ..config import read_config_file, update_config_values
    with _lock:
        value = read_config_file().get("routines", {})
        if not isinstance(value, dict):
            debug_log(f"routine {outcome} refused: the routines value is not an object", "routines")
            raise RoutineStoreError("The routines in config.json could not be read, so nothing was changed.")
        updated = change(copy.deepcopy(value))
        if not update_config_values({"routines": updated}):
            debug_log(f"routine {outcome} failed: config not written", "routines")
            raise RoutineStoreError("config.json could not be written, so nothing was changed.")
        loaded = reload()
    debug_log(f"routine {outcome}: ok ({len(loaded)} routines live)", "routines")
    return loaded


def _put(value: dict, name: str, definition: Optional[dict], *, remove=()) -> dict:
    """``value`` with every entry whose name normalises like ``name`` or one of ``remove`` taken out and
    ``definition`` under ``name`` where the first of them was (or last), keeping the order."""
    gone = {names.key(name), *(names.key(other) for other in remove)}
    result, placed = {}, definition is None
    for key, entry in value.items():
        if isinstance(key, str) and names.key(key) in gone:
            if not placed:
                result[name] = definition
                placed = True
            continue
        result[key] = entry
    if not placed:
        result[name] = definition
    return result


def _entry(value: dict, name: str):
    """The definition in the file for the routine ``name`` (file keys may differ in case or spacing)."""
    wanted = names.key(name)
    return next((entry for key, entry in value.items() if isinstance(key, str) and names.key(key) == wanted), None)


def save(name: str, steps: list[dict], *, replaces: Optional[str] = None) -> dict[str, Routine]:
    """Write a routine of ``steps``. When it replaces ``replaces``, that routine's aliases and any other
    keys it has are kept and only its steps change."""
    def change(value: dict) -> dict:
        previous = _entry(value, replaces) if replaces is not None else None
        kept = {key: entry for key, entry in previous.items() if key != "steps"} if isinstance(previous, dict) else {}
        return _put(value, name, {**kept, "steps": copy.deepcopy(steps)}, remove=(replaces,) if replaces else ())
    return _write(change, "save")


def delete(name: str) -> dict[str, Routine]:
    return _write(lambda value: _put(value, name, None), "delete")


def rename(name: str, new_name: str) -> dict[str, Routine]:
    """Move a routine to ``new_name``, keeping its aliases, steps and any other keys."""
    def change(value: dict) -> dict:
        definition = _entry(value, name)
        if definition is None:
            raise RoutineStoreError("That routine is no longer in config.json, so nothing was changed.")
        return _put(value, new_name, definition, remove=(name,))
    return _write(change, "rename")
