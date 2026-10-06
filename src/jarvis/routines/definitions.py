"""Routines configuration: structural loading, name and alias resolution, step labels.

Pure, no I/O. Whether a routine's tools exist and its arguments fit them is decided when it runs
(``runner.py``), because the tool catalogue depends on the platform, settings and MCP servers of the
moment. See ``routines.spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Optional

from ..debug import debug_log
from ..utils import names

MAX_STEPS = 20
# Never a step and never recorded in the journal: routines do not nest, and conversation control and
# tool routing are not actions.
NEVER_STEPS = frozenset({"routineControl", "stop", "toolSearchTool", "refreshMCPTools"})
_SHORT_TEXT = 40


@dataclass(frozen=True)
class Step:
    tool: str
    args: Mapping[str, Any]
    label: Optional[str] = None


@dataclass(frozen=True)
class Routine:
    name: str
    aliases: tuple[str, ...]
    steps: tuple[Step, ...]


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load_step(raw) -> Optional[Step]:
    if not isinstance(raw, dict) or not _text(raw.get("tool")):
        return None
    args, label = raw.get("args", {}), raw.get("label")
    if not isinstance(args, dict) or (label is not None and not isinstance(label, str)):
        return None
    return Step(raw["tool"].strip(), dict(args), label.strip() if label and label.strip() else None)


def load_routines(value) -> dict[str, Routine]:
    """Keep only well-formed routines. Names and aliases that two routines claim are not offered.

    Dropped entries are counted in the debug log, never named; the user's file is untouched."""
    if not isinstance(value, dict):
        return {}
    loaded: dict[str, tuple[list[str], tuple[Step, ...]]] = {}
    dropped = 0
    for name, raw in value.items():
        steps = raw.get("steps") if isinstance(raw, dict) else None
        parsed = [_load_step(step) for step in steps] if isinstance(steps, list) else []
        if not _text(name) or not parsed or len(parsed) > MAX_STEPS or None in parsed:
            dropped += 1
            continue
        aliases = raw.get("aliases")
        aliases = [alias.strip() for alias in aliases if _text(alias)] if isinstance(aliases, list) else []
        loaded[name.strip()] = (aliases, tuple(parsed))
    claimed, contested = names.drop_contested({name: aliases for name, (aliases, _) in loaded.items()})
    if dropped or contested:
        debug_log(f"routines loaded: {len(claimed)} kept, {dropped} malformed and {contested} contested names "
                  f"or aliases dropped", "routines")
    return {name: Routine(name, tuple(aliases), loaded[name][1]) for name, aliases in claimed.items()}


def find(name, routines: Mapping[str, Routine]) -> Optional[Routine]:
    """The routine that ``name`` names or is an alias of, ignoring case."""
    found = names.resolve(name, {routine.name: routine.aliases for routine in routines.values()})
    return routines.get(found) if found is not None else None


def _shown(value) -> Optional[str]:
    """A short text or number argument as it appears in a label, or ``None`` when it is left out."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    # A path or a web address never appears, wherever it is in the arguments.
    if not text or len(text) > _SHORT_TEXT or any(mark in text for mark in "/\\:") or text[0] in "~%":
        return None
    return text


def step_label(step: Step, schema: Optional[Mapping[str, Any]] = None) -> str:
    """The label results, lists and read-backs show for a step; arguments themselves are never shown.

    A given ``label`` as written; otherwise the tool name, the ``action`` value, then the short text and
    number values of the other arguments in the order of the tool's schema."""
    if step.label:
        return step.label
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    order = [key for key in (properties or {}) if key in step.args]
    order += [key for key in step.args if key not in order]
    if "action" in order:
        order.remove("action")
        order.insert(0, "action")
    parts = [step.tool, *(shown for shown in map(_shown, (step.args[key] for key in order)) if shown)]
    return " ".join(parts)


def fingerprint(routine: Routine) -> str:
    """Identifies a routine's steps, so an approval given for one version never runs another."""
    canonical = json.dumps([[step.tool, step.args, step.label] for step in routine.steps],
                           sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
