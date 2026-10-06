"""Opt-in activity log: storage, recording rules and the aggregates the tool returns.

The recorder is driven with synthetic observations (foreground application, title, idle seconds and a
clock), so nothing here touches the screen, a window or a device. See ``memory/activity_log.spec.md``.
"""
import json
import sqlite3
import threading

import pytest

from jarvis.memory import activity_log as al
from jarvis.memory.activity_log import (
    ActivityRecorder, ActivityStore, Observation, summarise, timeline,
)

T0 = 1_760_000_000.0  # an arbitrary fixed instant; only differences matter
CODE = Observation(process="Code", app="Visual Studio Code", title="spec.md - jarvis")
CHROME = Observation(process="chrome", app="Google Chrome", title="Inbox - Mail")
PASSWORDS = Observation(process="1Password", app="1Password", title="Vault")


@pytest.fixture
def store(tmp_path):
    s = ActivityStore(str(tmp_path / "jarvis.db"))
    yield s
    s.close()


def recorder(store, **overrides):
    options = dict(excluded_processes=al.default_excluded_processes(),
                   private_markers=al.default_private_title_markers(), idle_after_sec=300.0)
    options.update(overrides)
    return ActivityRecorder(store, **options)


def rows(store, start=0.0, end=T0 + 10**6):
    return [(s.process, s.title, s.idle, round(s.end - s.start, 1)) for s in store.sessions(start, end)]


class TestStore:
    def test_adds_its_own_table_next_to_the_existing_database(self, tmp_path):
        path = str(tmp_path / "jarvis.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meals (id INTEGER PRIMARY KEY, description TEXT)")
        conn.execute("INSERT INTO meals(description) VALUES ('toast')")
        conn.commit()
        conn.close()

        ActivityStore(path).close()

        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "meals" in tables and any("activity" in t for t in tables)
        assert conn.execute("SELECT description FROM meals").fetchall() == [("toast",)]
        conn.close()

    def test_returns_only_sessions_overlapping_the_range_in_order(self, store):
        store.open_session(T0 + 100, "a", "A", "a-title", end=T0 + 200)
        store.open_session(T0, "b", "B", "b-title", end=T0 + 50)
        store.open_session(T0 + 500, "c", "C", "c-title", end=T0 + 600)

        found = store.sessions(T0 + 40, T0 + 150)

        assert [s.process for s in found] == ["b", "a"]

    def test_prune_removes_only_sessions_that_ended_before_the_cutoff(self, store):
        store.open_session(T0, "old", "Old", "t", end=T0 + 10)
        store.open_session(T0 + 100, "new", "New", "t", end=T0 + 200)

        removed = store.prune(T0 + 50)

        assert removed == 1
        assert [s.process for s in store.sessions(0, T0 + 10**6)] == ["new"]

    def test_delete_all_empties_the_table(self, store):
        store.open_session(T0, "a", "A", "t", end=T0 + 10)
        assert store.delete_all() == 1
        assert store.sessions(0, T0 + 10**6) == []

    def test_deleted_titles_are_not_left_readable_in_the_database_file(self, tmp_path):
        from pathlib import Path
        path = tmp_path / "jarvis.db"
        s = ActivityStore(str(path))
        for i in range(20):
            s.open_session(T0 + i, "proc", "App", f"confidential-quarterly-plan-{i}", end=T0 + i + 5)
        s.delete_all()
        s.close()

        leftovers = b"".join(p.read_bytes() for p in tmp_path.glob("jarvis.db*"))
        assert b"confidential-quarterly-plan" not in leftovers

    def test_usable_from_another_thread(self, store):
        sid = store.open_session(T0, "a", "A", "t", end=T0 + 1)
        errors = []

        def work():
            try:
                store.update_end(sid, T0 + 9)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        assert not errors
        assert store.sessions(0, T0 + 10**6)[0].end == T0 + 9


class TestRecording:
    def test_records_each_foreground_application_as_a_session(self, store):
        rec = recorder(store)
        rec.observe(CODE, 0, T0)
        rec.observe(CODE, 0, T0 + 60)
        rec.observe(CHROME, 0, T0 + 60)
        rec.observe(CHROME, 0, T0 + 90)
        rec.close(T0 + 90)

        assert rows(store) == [("Code", "spec.md - jarvis", False, 60.0),
                               ("chrome", "Inbox - Mail", False, 30.0)]

    def test_a_title_change_in_the_same_application_starts_a_new_session(self, store):
        rec = recorder(store)
        rec.observe(CODE, 0, T0)
        rec.observe(Observation("Code", "Visual Studio Code", "other.py - jarvis"), 0, T0 + 30)
        rec.close(T0 + 60)

        assert [r[1] for r in rows(store)] == ["spec.md - jarvis", "other.py - jarvis"]

    def test_idle_time_is_recorded_as_idle_and_not_as_the_last_application(self, store):
        rec = recorder(store, idle_after_sec=300)
        rec.observe(CODE, 0, T0)
        rec.observe(CODE, 0, T0 + 120)        # typing until here
        rec.observe(CODE, 200, T0 + 320)      # still under the threshold
        rec.observe(CODE, 400, T0 + 520)      # over it: last input was at T0 + 120
        rec.observe(CODE, 700, T0 + 820)
        rec.close(T0 + 820)

        sessions = store.sessions(0, T0 + 10**6)
        assert [(s.idle, round(s.start - T0), round(s.end - T0)) for s in sessions] == [
            (False, 0, 120), (True, 120, 820)]
        assert sessions[1].process == "" and sessions[1].title == ""

    def test_application_time_resumes_when_input_returns(self, store):
        rec = recorder(store, idle_after_sec=300)
        rec.observe(CODE, 0, T0)
        rec.observe(CODE, 1000, T0 + 1000)     # idle since T0
        rec.observe(CODE, 5, T0 + 1005)        # input at T0 + 1000
        rec.close(T0 + 1065)

        sessions = store.sessions(0, T0 + 10**6)
        idle = [s for s in sessions if s.idle]
        active = [s for s in sessions if not s.idle]
        assert len(idle) == 1 and idle[0].end == pytest.approx(T0 + 1000)
        assert active[-1].start == pytest.approx(T0 + 1000)
        assert active[-1].end == pytest.approx(T0 + 1065)

    def test_time_asleep_is_not_credited_to_the_window_left_open(self, store):
        """Sleep stops the ticks; waking with a key press reads no idle time. The night is a gap."""
        rec = recorder(store, idle_after_sec=300)
        rec.observe(CODE, 0, T0)
        rec.observe(CODE, 0, T0 + 5)
        night = 8 * 3600
        rec.observe(CODE, 0, T0 + 5 + night)        # woken by a key press
        rec.observe(CODE, 0, T0 + 10 + night)
        rec.close(T0 + 10 + night)

        sessions = store.sessions(0, T0 + 10**6)
        assert sum(s.end - s.start for s in sessions) == pytest.approx(10)
        assert [round(s.start - T0) for s in sessions] == [0, 5 + night]

    def test_a_long_gap_while_idle_stays_idle(self, store):
        rec = recorder(store, idle_after_sec=300)
        rec.observe(CODE, 0, T0)
        rec.observe(CODE, 400, T0 + 400)            # idle since T0
        rec.observe(CODE, 4000, T0 + 4000)          # asleep in between, no input throughout
        rec.observe(CODE, 2, T0 + 4002)
        rec.close(T0 + 4010)

        idle = [s for s in store.sessions(0, T0 + 10**6) if s.idle]
        assert [(round(s.start - T0), round(s.end - T0)) for s in idle] == [(0, 4000)]

    def test_excluded_processes_leave_a_gap_with_no_trace(self, store):
        rec = recorder(store)
        rec.observe(CODE, 0, T0)
        rec.observe(PASSWORDS, 0, T0 + 30)
        rec.observe(PASSWORDS, 0, T0 + 90)
        rec.observe(CHROME, 0, T0 + 120)
        rec.close(T0 + 150)

        found = store.sessions(0, T0 + 10**6)
        assert [s.process for s in found] == ["Code", "chrome"]
        assert found[0].end == pytest.approx(T0 + 30)
        assert "1password" not in json.dumps([vars(s) for s in found]).lower()

    def test_process_exclusion_ignores_case_spacing_and_extension(self, store):
        rec = recorder(store, excluded_processes=["Proton Pass"])
        rec.observe(Observation("ProtonPass.exe", "Proton Pass", "x"), 0, T0)
        rec.close(T0 + 60)
        assert store.sessions(0, T0 + 10**6) == []

    @pytest.mark.parametrize("title", [
        "New tab - Google Chrome (Incognito)",
        "Docs - InPrivate - Microsoft Edge",
        "Mozilla Firefox Private Browsing",
        "Nouvel onglet - Navigation privée",
        "Neuer Tab – Inkognito",
    ])
    def test_private_browsing_titles_are_never_recorded(self, store, title):
        rec = recorder(store)
        rec.observe(Observation("chrome", "Google Chrome", title), 0, T0)
        rec.close(T0 + 60)
        assert store.sessions(0, T0 + 10**6) == []

    def test_the_marker_and_process_lists_are_configurable(self, store):
        rec = recorder(store, excluded_processes=["notepad"], private_markers=["secret project"])
        rec.observe(Observation("notepad", "Notepad", "a"), 0, T0)
        rec.observe(Observation("Code", "Code", "Secret Project plan"), 0, T0 + 10)
        rec.observe(CHROME, 0, T0 + 20)
        rec.close(T0 + 80)
        assert [s.process for s in store.sessions(0, T0 + 10**6)] == ["chrome"]

    def test_titles_are_redacted_and_bounded_before_storage(self, store):
        title = "Mail to alice@example.com " + "x" * 500
        rec = recorder(store)
        rec.observe(Observation("chrome", "Google Chrome", title), 0, T0)
        rec.close(T0 + 60)

        stored = store.sessions(0, T0 + 10**6)[0].title
        assert "alice@example.com" not in stored
        assert len(stored) <= al.TITLE_MAX_CHARS

    def test_nothing_is_recorded_while_paused(self, store):
        rec = recorder(store)
        rec.observe(CODE, 0, T0)
        rec.pause(T0 + 30)
        rec.observe(CHROME, 0, T0 + 60)
        rec.observe(CHROME, 0, T0 + 120)
        assert rec.paused
        rec.resume()
        rec.observe(CHROME, 0, T0 + 150)
        rec.close(T0 + 210)

        found = store.sessions(0, T0 + 10**6)
        assert [(s.process, round(s.start - T0), round(s.end - T0)) for s in found] == [
            ("Code", 0, 30), ("chrome", 150, 210)]

    def test_no_foreground_window_ends_the_session(self, store):
        rec = recorder(store)
        rec.observe(CODE, 0, T0)
        rec.observe(None, 0, T0 + 40)
        rec.observe(None, 0, T0 + 100)
        rec.close(T0 + 100)
        found = store.sessions(0, T0 + 10**6)
        assert [(s.process, round(s.end - s.start)) for s in found] == [("Code", 40)]

    def test_flicker_shorter_than_the_minimum_is_not_kept(self, store):
        rec = recorder(store, min_session_sec=2.0)
        rec.observe(CODE, 0, T0)
        rec.observe(CHROME, 0, T0 + 60)
        rec.observe(CODE, 0, T0 + 60.5)
        rec.close(T0 + 120)
        assert [s.process for s in store.sessions(0, T0 + 10**6)] == ["Code", "Code"]

    def test_deleting_history_while_recording_starts_clean(self, store):
        rec = recorder(store)
        rec.observe(CODE, 0, T0)
        rec.observe(CODE, 0, T0 + 60)

        removed = rec.delete_history()
        rec.observe(CODE, 0, T0 + 90)
        rec.observe(CODE, 0, T0 + 150)
        rec.close(T0 + 150)

        assert removed >= 1
        found = store.sessions(0, T0 + 10**6)
        assert len(found) == 1 and found[0].start == pytest.approx(T0 + 90)

    def test_an_open_session_is_saved_as_it_grows(self, store):
        rec = recorder(store)
        rec.observe(CODE, 0, T0)
        rec.observe(CODE, 0, T0 + 45)
        # No close: the process may have died here.
        assert store.sessions(0, T0 + 10**6)[0].end == pytest.approx(T0 + 45)


class TestDefaults:
    def test_password_managers_are_excluded_by_default(self):
        names = {n.lower() for n in al.default_excluded_processes()}
        assert {"1password", "bitwarden", "keepass"} <= names

    def test_private_browsing_markers_cover_several_languages(self):
        markers = {m.lower() for m in al.default_private_title_markers()}
        assert "incognito" in markers and "inprivate" in markers
        assert len(markers) > 6


class TestAggregates:
    def seed(self, store):
        # Two hours of VS Code on two files, an hour of Chrome, thirty minutes idle.
        store.open_session(T0, "Code", "Visual Studio Code", "a.py", end=T0 + 3600)
        store.open_session(T0 + 3600, "Code", "Visual Studio Code", "b.py", end=T0 + 5400)
        store.open_session(T0 + 5400, "chrome", "Google Chrome", "Docs", end=T0 + 9000)
        store.open_session(T0 + 9000, "", "", "", idle=True, end=T0 + 10800)

    def test_summary_totals_per_application_and_title(self, store):
        self.seed(store)

        result = summarise(store, T0, T0 + 10800)

        assert result["active_seconds"] == 9000 and result["idle_seconds"] == 1800
        apps = {a["app"]: a for a in result["apps"]}
        assert [a["app"] for a in result["apps"]] == ["Visual Studio Code", "Google Chrome"]
        assert apps["Visual Studio Code"]["seconds"] == 5400
        assert [(t["title"], t["seconds"]) for t in apps["Visual Studio Code"]["titles"]] == [
            ("a.py", 3600), ("b.py", 1800)]

    def test_summary_clips_sessions_to_the_requested_range(self, store):
        self.seed(store)

        result = summarise(store, T0 + 1800, T0 + 4500)

        apps = {a["app"]: a["seconds"] for a in result["apps"]}
        assert apps == {"Visual Studio Code": 2700}

    def test_summary_of_an_empty_range_says_when_recording_began(self, store):
        self.seed(store)

        result = summarise(store, T0 - 7200, T0 - 3600)

        assert result["apps"] == [] and result["active_seconds"] == 0
        assert result["recording_since"].startswith("20")

    def test_summary_with_no_data_at_all(self, store):
        result = summarise(store, T0, T0 + 100)
        assert result["apps"] == [] and result["recording_since"] is None

    def test_timeline_is_ordered_and_clipped(self, store):
        self.seed(store)

        result = timeline(store, T0 + 3000, T0 + 6000)

        assert [(s["app"], s["seconds"]) for s in result["sessions"]] == [
            ("Visual Studio Code", 600), ("Visual Studio Code", 1800), ("Google Chrome", 600)]
        assert result["sessions"][0]["start"] < result["sessions"][1]["start"]
        assert result["truncated"] is False

    def test_timeline_marks_idle_and_respects_the_limit(self, store):
        self.seed(store)

        result = timeline(store, T0, T0 + 10800, limit=2)

        assert len(result["sessions"]) == 2 and result["truncated"] is True
        full = timeline(store, T0, T0 + 10800)
        assert full["sessions"][-1]["idle"] is True

    def test_aggregates_redact_titles_again(self, store):
        store.open_session(T0, "chrome", "Google Chrome", "Mail to bob@example.org", end=T0 + 60)

        text = json.dumps(summarise(store, T0, T0 + 60)) + json.dumps(timeline(store, T0, T0 + 60))

        assert "bob@example.org" not in text

    def test_summary_bounds_the_number_of_apps_and_titles(self, store):
        for i in range(30):
            store.open_session(T0 + i * 10, f"p{i}", f"App {i}", f"t{i}", end=T0 + i * 10 + 9)
        for i in range(12):
            store.open_session(T0 + 400 + i, "many", "Many", f"title {i}", end=T0 + 400 + i + 1)

        result = summarise(store, T0, T0 + 1000)

        assert len(result["apps"]) <= al.SUMMARY_MAX_APPS
        many = [a for a in result["apps"] if a["app"] == "Many"]
        assert not many or len(many[0]["titles"]) <= al.SUMMARY_MAX_TITLES


class TestTurnPrivacyFlag:
    def test_marking_is_per_thread_and_consumed_once(self):
        al.consume_turn_private()
        assert al.consume_turn_private() is False
        al.mark_turn_private()
        seen = []
        thread = threading.Thread(target=lambda: seen.append(al.consume_turn_private()))
        thread.start()
        thread.join()
        assert seen == [False]
        assert al.consume_turn_private() is True
        assert al.consume_turn_private() is False


class TestTimeParsing:
    def test_naive_times_are_local_and_offsets_are_honoured(self):
        from datetime import datetime, timezone
        expected_local = datetime(2026, 10, 3, 14, 0, 0).astimezone().timestamp()
        expected_utc = datetime(2026, 10, 3, 14, 0, 0, tzinfo=timezone.utc).timestamp()
        assert al.parse_time("2026-10-03T14:00:00") == pytest.approx(expected_local)
        assert al.parse_time("2026-10-03T14:00:00+00:00") == pytest.approx(expected_utc)
        assert al.parse_time("2026-10-03T14:00:00Z") == pytest.approx(expected_utc)

    def test_garbage_is_rejected(self):
        with pytest.raises(ValueError):
            al.parse_time("yesterday afternoon")


class TestBundling:
    def test_the_desktop_bundle_ships_the_exclusion_data_file(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        assert (root / "src" / "jarvis" / "memory" / "activity_exclusions.json").is_file()
        assert "activity_exclusions.json" in (root / "jarvis_desktop.spec").read_text(encoding="utf-8")


class TestExclusionDataFileMissing:
    def test_a_missing_data_file_still_excludes_password_managers_and_private_windows(self, monkeypatch, tmp_path):
        monkeypatch.setattr(al, "_DEFAULTS_FILE", tmp_path / "missing.json")
        al._defaults.cache_clear()
        try:
            names = {n.lower() for n in al.default_excluded_processes()}
            markers = {m.lower() for m in al.default_private_title_markers()}
        finally:
            al._defaults.cache_clear()
        assert {"1password", "bitwarden", "keepass"} <= names
        assert {"incognito", "inprivate"} <= markers
