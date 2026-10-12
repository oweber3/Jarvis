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


# Language-neutral day codes for schedules, Monday first (``datetime.weekday`` order).
DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# What a run reports: ``actions`` (labels and outcomes) or ``results`` (also what the steps found: a briefing).
REPORTS = ("actions", "results")


@dataclass(frozen=True)
class Schedule:
    """When a routine runs on its own: a local time of day on some days of the week."""
    hour: int
    minute: int
    days: frozenset  # weekday numbers, Monday 0
    enabled: bool = True

    def as_config(self) -> dict:
        value = {"time": f"{self.hour:02d}:{self.minute:02d}"}
        if self.days != frozenset(range(7)):
            value["days"] = [DAY_CODES[day] for day in sorted(self.days)]
        value["enabled"] = self.enabled
        return value


@dataclass(frozen=True)
class Routine:
    name: str
    aliases: tuple[str, ...]
    steps: tuple[Step, ...]
    schedule: Optional[Schedule] = None
    report: str = "actions"


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load_step(raw) -> Optional[Step]:
    if not isinstance(raw, dict) or not _text(raw.get("tool")):
        return None
    args, label = raw.get("args", {}), raw.get("label")
    if not isinstance(args, dict) or (label is not None and not isinstance(label, str)):
        return None
    return Step(raw["tool"].strip(), dict(args), label.strip() if label and label.strip() else None)


def parse_time(value) -> Optional[tuple[int, int]]:
    """``HH:MM`` (24-hour) as hour and minute, or ``None``."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    return (hour, minute) if hour < 24 and minute < 60 else None


def parse_days(value) -> Optional[frozenset]:
    """A non-empty list of day codes as weekday numbers; ``None`` (omitted) means every day."""
    if value is None:
        return frozenset(range(7))
    if not isinstance(value, list) or not value:
        raise ValueError("days must be a non-empty list of day codes")
    days = set()
    for code in value:
        key = code.strip().casefold() if isinstance(code, str) else None
        if key not in DAY_CODES:
            raise ValueError("unknown day code")
        days.add(DAY_CODES.index(key))
    return frozenset(days)


def load_schedule(raw) -> Optional[Schedule]:
    """A routine's ``schedule`` value, or ``None`` when it is absent or malformed."""
    if not isinstance(raw, dict):
        return None
    time = parse_time(raw.get("time"))
    enabled = raw.get("enabled", True)
    if time is None or not isinstance(enabled, bool):
        return None
    try:
        days = parse_days(raw.get("days"))
    except ValueError:
        return None
    return Schedule(time[0], time[1], days, enabled)


def load_routines(value) -> dict[str, Routine]:
    """Keep only well-formed routines. Names and aliases that two routines claim are not offered.

    Dropped entries are counted in the debug log, never named; the user's file is untouched."""
    if not isinstance(value, dict):
        return {}
    loaded: dict[str, tuple] = {}
    dropped = bad_schedules = 0
    for name, raw in value.items():
        steps = raw.get("steps") if isinstance(raw, dict) else None
        parsed = [_load_step(step) for step in steps] if isinstance(steps, list) else []
        if not _text(name) or not parsed or len(parsed) > MAX_STEPS or None in parsed:
            dropped += 1
            continue
        aliases = raw.get("aliases")
        aliases = [alias.strip() for alias in aliases if _text(alias)] if isinstance(aliases, list) else []
        schedule = load_schedule(raw.get("schedule"))
        if schedule is None and raw.get("schedule") is not None:
            bad_schedules += 1
        report = raw.get("report") if raw.get("report") in REPORTS else "actions"
        loaded[name.strip()] = (aliases, tuple(parsed), schedule, report)
    claimed, contested = names.drop_contested({name: entry[0] for name, entry in loaded.items()})
    if dropped or contested or bad_schedules:
        debug_log(f"routines loaded: {len(claimed)} kept, {dropped} malformed and {contested} contested names "
                  f"or aliases dropped, {bad_schedules} malformed schedules ignored", "routines")
    return {name: Routine(name, tuple(aliases), *loaded[name][1:]) for name, aliases in claimed.items()}


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
