"""The activity service: opt-in start, event handling, pause, delete, pruning and shutdown.

Driven with a fake watcher, synthetic foreground readings and a fake clock, so no hook, window or
timer is involved. See ``memory/activity_log.spec.md``.
"""
from types import SimpleNamespace

import pytest

from jarvis.memory import activity_log as al
from jarvis.memory import activity_runtime as runtime
from jarvis.memory.activity_log import ActivityStore, Observation

T0 = 1_760_000_000.0
CODE = Observation("Code", "Visual Studio Code", "spec.md - jarvis")
CHROME = Observation("chrome", "Google Chrome", "Inbox")


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeWatcher:
    def __init__(self, on_event):
        self.on_event = on_event
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        self.on_event("tick")  # like the real watcher, which reports the current window straight away
        return True

    def stop(self):
        self.stopped += 1


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(
        activity_log_enabled=True, activity_log_paused=False, activity_log_retention_days=30,
        activity_log_idle_after_sec=300.0, activity_log_excluded_processes=["1Password"],
        activity_log_private_title_markers=["Incognito"], db_path=str(tmp_path / "jarvis.db"))


@pytest.fixture(autouse=True)
def stopped():
    runtime.stop()
    yield
    runtime.stop()


class Rig:
    def __init__(self, cfg, foreground=CODE):
        self.clock = Clock()
        self.foreground = foreground
        self.idle = 0.0
        self.watchers = []

        def factory(on_event):
            watcher = FakeWatcher(on_event)
            self.watchers.append(watcher)
            return watcher

        self.service = runtime.start(cfg, foreground=lambda: self.foreground, idle=lambda: self.idle,
                                     watcher_factory=factory, clock=self.clock)
        self.cfg = cfg

    @property
    def watcher(self):
        return self.watchers[-1]

    def event(self, kind="tick", advance=0.0):
        self.clock.advance(advance)
        self.watcher.on_event(kind)

    def sessions(self):
        store = ActivityStore(self.cfg.db_path)
        try:
            return store.sessions(0, T0 + 10**7)
        finally:
            store.close()


@pytest.mark.unit
class TestOptIn:
    def test_nothing_starts_while_the_log_is_off(self, cfg):
        cfg.activity_log_enabled = False
        rig = Rig(cfg)
        assert rig.service is None and rig.watchers == [] and runtime.get_service() is None

    def test_only_a_real_true_starts_it(self, cfg):
        cfg.activity_log_enabled = "true"
        assert Rig(cfg).service is None

    def test_enabled_starts_one_watcher_and_records_the_current_window(self, cfg):
        rig = Rig(cfg)
        assert rig.service is not None and rig.watcher.started == 1
        rig.event(advance=30)
        rig.event(advance=30)
        assert [(s.process, round(s.end - s.start)) for s in rig.sessions()] == [("Code", 60)]

    def test_starting_again_replaces_the_service(self, cfg):
        rig = Rig(cfg)
        first = rig.watcher
        runtime.start(cfg, foreground=lambda: CODE, idle=lambda: 0.0,
                      watcher_factory=lambda cb: FakeWatcher(cb), clock=rig.clock)
        assert first.stopped == 1


@pytest.mark.unit
class TestEvents:
    def test_switching_application_starts_a_new_session(self, cfg):
        rig = Rig(cfg)
        rig.event("tick", 10)
        rig.foreground = CHROME
        rig.event("foreground", 20)
        rig.event("tick", 20)
        assert [s.process for s in rig.sessions()] == ["Code", "chrome"]

    def test_idle_is_read_from_the_system(self, cfg):
        rig = Rig(cfg)
        rig.event("tick", 100)      # typing until here
        rig.idle = 300.0
        rig.event("tick", 300)
        assert [(s.idle, round(s.end - s.start)) for s in rig.sessions()] == [(False, 100), (True, 300)]

    def test_excluded_applications_are_not_recorded(self, cfg):
        rig = Rig(cfg, foreground=Observation("1Password", "1Password", "Vault"))
        rig.event("foreground", 30)
        rig.event("tick", 30)
        assert rig.sessions() == []

    def test_a_failing_reading_does_not_kill_the_service_or_end_the_session(self, cfg):
        rig = Rig(cfg)
        rig.event("tick", 20)

        def broken():
            raise OSError("access denied")

        rig.service._foreground = broken
        rig.event("tick", 20)
        rig.service._foreground = lambda: CODE
        rig.event("tick", 20)
        assert [(s.process, round(s.end - s.start)) for s in rig.sessions()] == [("Code", 60)]

    def test_logs_never_carry_titles_or_applications(self, cfg, monkeypatch):
        logged = []
        for module in (runtime, al):
            monkeypatch.setattr(module, "debug_log", lambda message, *a, **k: logged.append(message))
        rig = Rig(cfg)
        rig.event("tick", 30)
        runtime.set_paused(True, persist=False)
        runtime.set_paused(False, persist=False)
        runtime.delete_history(cfg)
        text = " ".join(logged)
        assert logged and "spec.md" not in text and "Visual Studio" not in text and "jarvis" not in text.replace(
            "activity log", "")


@pytest.mark.unit
class TestPause:
    def test_pausing_stops_recording_and_resuming_continues(self, cfg, monkeypatch):
        saved = []
        monkeypatch.setattr("jarvis.config.update_config_values", lambda values: saved.append(values) or True)
        rig = Rig(cfg)
        rig.event("tick", 30)

        assert runtime.set_paused(True) is True
        rig.foreground = CHROME
        rig.event("foreground", 60)
        rig.event("tick", 60)
        runtime.set_paused(False)
        rig.event("tick", 30)
        rig.event("tick", 30)

        assert saved == [{"activity_log_paused": True}, {"activity_log_paused": False}]
        found = [(s.process, round(s.end - s.start)) for s in rig.sessions()]
        assert found[0] == ("Code", 30)
        assert [p for p, _ in found] == ["Code", "chrome"]

    def test_a_paused_start_records_nothing_until_resumed(self, cfg, monkeypatch):
        monkeypatch.setattr("jarvis.config.update_config_values", lambda values: True)
        cfg.activity_log_paused = True
        rig = Rig(cfg)
        rig.event("tick", 30)
        rig.event("tick", 30)
        assert rig.sessions() == []
        runtime.set_paused(False)
        rig.event("tick", 30)
        assert [s.process for s in rig.sessions()] == ["Code"]


    def test_nothing_is_read_from_the_desktop_while_paused(self, cfg, monkeypatch):
        """Paused means not observed: no title or idle read, not just nothing stored."""
        monkeypatch.setattr("jarvis.config.update_config_values", lambda values: True)
        reads = []
        rig = Rig(cfg)
        rig.service._foreground = lambda: reads.append("foreground") or rig.foreground
        rig.service._idle = lambda: reads.append("idle") or 0.0
        runtime.set_paused(True)
        for _ in range(5):
            rig.event("tick", 5)
        assert reads == []
        runtime.set_paused(False)
        assert "foreground" in reads

    def test_retention_is_still_applied_while_paused(self, cfg, monkeypatch):
        monkeypatch.setattr("jarvis.config.update_config_values", lambda values: True)
        rig = Rig(cfg)
        rig.event("tick", 30)
        runtime.set_paused(True)
        rig.event("tick", cfg.activity_log_retention_days * 86400 + runtime.PRUNE_INTERVAL_SEC + 60)
        assert rig.sessions() == []


@pytest.mark.unit
class TestDelete:
    def test_deleting_while_recording_removes_everything_and_recording_continues(self, cfg):
        rig = Rig(cfg)
        rig.event("tick", 30)
        rig.event("tick", 30)
        assert rig.sessions()

        removed = runtime.delete_history(cfg)
        assert removed >= 1 and rig.sessions() == []

        rig.event("tick", 30)
        rig.event("tick", 30)
        assert [s.process for s in rig.sessions()] == ["Code"]

    def test_deleting_with_no_recording_running_still_clears_the_database(self, cfg):
        store = ActivityStore(cfg.db_path)
        store.open_session(T0, "Code", "Code", "t", end=T0 + 60)
        store.close()

        assert runtime.delete_history(cfg) == 1

        store = ActivityStore(cfg.db_path)
        assert store.sessions(0, T0 + 10**6) == []
        store.close()


@pytest.mark.unit
class TestRetention:
    def seed(self, cfg, days_old):
        store = ActivityStore(cfg.db_path)
        store.open_session(T0 - days_old * 86400, "old", "Old", "t", end=T0 - days_old * 86400 + 60)
        store.close()

    def test_old_sessions_are_pruned_at_start(self, cfg):
        self.seed(cfg, days_old=45)
        self.seed(cfg, days_old=2)
        rig = Rig(cfg)
        assert [round((T0 - s.start) / 86400) for s in rig.sessions() if s.process == "old"] == [2]

    def test_retention_is_configurable(self, cfg):
        cfg.activity_log_retention_days = 1
        self.seed(cfg, days_old=2)
        assert not any(s.process == "old" for s in Rig(cfg).sessions())

    def test_pruning_runs_again_after_a_day_of_running(self, cfg):
        rig = Rig(cfg)
        store = ActivityStore(cfg.db_path)
        store.open_session(rig.clock.now - 40 * 86400, "old", "Old", "t", end=rig.clock.now - 40 * 86400 + 60)
        store.close()
        rig.event("tick", 3600)
        assert any(s.process == "old" for s in rig.sessions())  # not yet
        rig.event("tick", 24 * 3600)
        assert not any(s.process == "old" for s in rig.sessions())


@pytest.mark.unit
class TestShutdown:
    def test_stopping_closes_the_open_session_and_the_watcher(self, cfg):
        rig = Rig(cfg)
        rig.event("tick", 30)
        rig.clock.advance(30)
        runtime.stop()
        assert rig.watcher.stopped == 1 and runtime.get_service() is None
        assert [(s.process, round(s.end - s.start)) for s in rig.sessions()] == [("Code", 60)]

    def test_events_after_stopping_are_ignored(self, cfg):
        rig = Rig(cfg)
        watcher = rig.watcher
        runtime.stop()
        watcher.on_event("tick")
        assert [round(s.end - s.start) for s in rig.sessions()] == []

    def test_stopping_with_nothing_running_is_harmless(self):
        runtime.stop()
        runtime.stop()
