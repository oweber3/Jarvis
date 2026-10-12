"""Scheduler wording: announcements, confirmations and spoken durations from locale phrase files.

Every sentence the scheduler says comes from ``phrases/<language>.json``; English is the fallback for a
language without a file or a key the file does not have. See ``scheduler.spec.md``, Wording.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Iterable, Optional

FALLBACK = "en"
_UNITS = (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1))


def language_of(language: Optional[str]) -> str:
    """The bare language code (``en`` for ``en-GB``), or the fallback."""
    code = (language or FALLBACK).casefold().replace("_", "-").split("-")[0]
    return code if re.fullmatch(r"[a-z]{2,3}", code) else FALLBACK


@lru_cache(maxsize=8)
def _table(language: str) -> dict:
    path = Path(__file__).parent / "phrases" / f"{language}.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _value(key: str, language: Optional[str]):
    table = _table(language_of(language))
    return table[key] if key in table else _table(FALLBACK)[key]


def fragment(key: str, language: Optional[str], **values) -> str:
    """A template filled in, as written (for parts of a sentence)."""
    return str(_value(key, language)).format(**values)


def text(key: str, language: Optional[str], **values) -> str:
    """A whole sentence: the template filled in, starting with a capital letter."""
    filled = fragment(key, language, **values).strip()
    return filled[:1].upper() + filled[1:]


def join(items: Iterable[str], language: Optional[str]) -> str:
    items = [item for item in items if item]
    if len(items) <= 1:
        return "".join(items)
    return (_value("list_joiner", language).join(items[:-1]) + _value("list_last_joiner", language) + items[-1])


def format_duration(seconds: float, language: Optional[str]) -> str:
    """``10 minutes``, ``1 minute and 30 seconds``; seconds are left out once there are hours."""
    remaining = max(1, int(round(seconds)))
    units = _value("units", language)
    parts = []
    for name, size in _UNITS:
        if name == "second" and parts and parts[0][1] in ("day", "hour"):
            break
        count, remaining = divmod(remaining, size)
        if count:
            parts.append((count, name))
    return join([f"{count} {units[name][0 if count == 1 else 1]}" for count, name in parts], language)


def clock_time(timestamp: float, language: Optional[str]) -> str:
    return datetime.fromtimestamp(timestamp).strftime(_value("time_format", language))


def calendar_date(timestamp: float, language: Optional[str]) -> str:
    return datetime.fromtimestamp(timestamp).strftime(_value("date_format", language))


def format_when(timestamp: float, now: float, language: Optional[str]) -> str:
    """``at 14:30`` today, ``tomorrow at 09:00``, otherwise the date and time (local)."""
    days = (datetime.fromtimestamp(timestamp).date() - datetime.fromtimestamp(now).date()).days
    time_text = clock_time(timestamp, language)
    if days <= 0:
        return fragment("when_today", language, time=time_text)
    if days == 1:
        return fragment("when_tomorrow", language, time=time_text)
    return fragment("when_date", language, date=calendar_date(timestamp, language), time=time_text)
