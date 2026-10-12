"""Scheduler wording comes from locale phrase files, with English as the fallback."""
import json
from pathlib import Path

import pytest

from jarvis.scheduler import phrases
from scheduler_fakes import local_ts

PHRASES = Path(phrases.__file__).parent / "phrases"


def _units(language="en"):
    return json.loads((PHRASES / f"{language}.json").read_text(encoding="utf-8"))["units"]


@pytest.mark.parametrize("seconds,parts", [
    (600, [(10, "minute")]),
    (1, [(1, "second")]),
    (90, [(1, "minute"), (30, "second")]),
    (3600 + 120, [(1, "hour"), (2, "minute")]),
    (3600 + 125, [(1, "hour"), (2, "minute")]),  # seconds are dropped once there are hours
    (2 * 86400 + 3600, [(2, "day"), (1, "hour")]),
])
def test_durations_are_spoken_with_the_locale_unit_words(seconds, parts):
    units = _units()
    words = [f"{n} {units[unit][0 if n == 1 else 1]}" for n, unit in parts]
    assert phrases.format_duration(seconds, "en") == phrases.join(words, "en")


def test_an_unknown_language_falls_back_to_english():
    assert phrases.text("timer_done", "xx", label="pasta") == phrases.text("timer_done", "en", label="pasta")
    assert phrases.format_duration(60, None) == phrases.format_duration(60, "en")


def test_sentences_start_with_a_capital_letter():
    assert phrases.text("timer_set", "en", label="pasta", duration="1 minute")[0].isupper()


def test_every_template_value_is_text_and_units_have_two_forms():
    for path in PHRASES.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for unit, forms in data["units"].items():
            assert len(forms) == 2, (path.name, unit)
        for key, value in data.items():
            if key != "units":
                assert isinstance(value, str), (path.name, key)


def test_when_names_today_tomorrow_or_the_date():
    now = local_ts(2026, 10, 12, 12, 0)
    today, tomorrow, later = (local_ts(2026, 10, 12, 14, 30), local_ts(2026, 10, 13, 9, 0),
                              local_ts(2026, 10, 20, 9, 0))
    assert phrases.format_when(today, now, "en") == phrases.fragment(
        "when_today", "en", time=phrases.clock_time(today, "en"))
    assert phrases.format_when(tomorrow, now, "en") == phrases.fragment(
        "when_tomorrow", "en", time=phrases.clock_time(tomorrow, "en"))
    assert phrases.format_when(later, now, "en") == phrases.fragment(
        "when_date", "en", date=phrases.calendar_date(later, "en"), time=phrases.clock_time(later, "en"))
