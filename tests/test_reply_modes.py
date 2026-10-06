"""Reply modes: start from config, switch at runtime among allowed modes, persist, and never start a
cloud bridge that was not allowed."""
from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from jarvis.bridge import modes, runtime


class FakeService:
    def __init__(self, mode, prepare_result=None, busy=False):
        self.mode = mode
        self.prepare_result = prepare_result
        self.busy = busy
        self.closed = 0
        self.cancelled = []
        self.prepared = threading.Event()

    def prepare(self):
        self.prepared.set()
        return self.prepare_result

    def is_busy(self):
        return self.busy

    def cancel_active(self, reason):
        self.cancelled.append(reason)
        return self.busy

    def close(self):
        self.closed += 1


class Factories:
    def __init__(self, **prepare_results):
        self.built = []
        self.prepare_results = prepare_results
        self.fail = set()

    def make(self, mode):
        def factory(cfg):
            if mode in self.fail:
                raise RuntimeError("boom")
            service = FakeService(mode, self.prepare_results.get(mode))
            self.built.append(service)
            return service
        return factory

    def table(self):
        return {"codex": self.make("codex"), "claude": self.make("claude")}


def cfg(**kw):
    base = dict(reply_mode="local", codex_enabled=False, claude_enabled=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def saved():
    return []


@pytest.fixture(autouse=True)
def clean():
    modes.reset()
    yield
    modes.reset()


def start(config, factories, saved):
    return modes.start(config, factories=factories.table(), save=lambda values: saved.append(values) or True)


@pytest.mark.unit
class TestStart:
    def test_local_mode_starts_and_imports_no_bridge(self, saved, monkeypatch):
        for name in list(sys.modules):
            if name.startswith(("jarvis.codex_bridge", "jarvis.claude_bridge")):
                monkeypatch.delitem(sys.modules, name)
        factories = Factories()
        assert modes.start(cfg(), save=lambda v: saved.append(v)) == "local"
        assert modes.active_mode() == "local" and runtime.get_service() is None and factories.built == []
        assert not any(name.startswith(("jarvis.codex_bridge", "jarvis.claude_bridge")) for name in sys.modules)

    def test_an_allowed_cloud_mode_starts_its_bridge_and_warms_it(self, saved, capsys):
        factories = Factories()
        assert start(cfg(reply_mode="claude", claude_enabled=True), factories, saved) == "claude"
        (service,) = factories.built
        assert runtime.get_service() is service and service.mode == "claude"
        assert modes.wait_for_warm_up(5) and service.prepared.is_set()
        out = capsys.readouterr().out
        assert "Anthropic" in out and "Claude" in out
        assert saved == []

    def test_a_cloud_mode_that_is_not_allowed_starts_as_local(self, saved, capsys):
        factories = Factories()
        assert start(cfg(reply_mode="codex", codex_enabled=False), factories, saved) == "local"
        assert factories.built == [] and runtime.get_service() is None
        assert "not allowed" in capsys.readouterr().out.lower()

    def test_warm_up_problems_are_reported_and_the_mode_stays(self, saved, capsys):
        factories = Factories(codex="signed_out")
        start(cfg(reply_mode="codex", codex_enabled=True), factories, saved)
        assert modes.wait_for_warm_up(5)
        assert modes.active_mode() == "codex"
        assert "signed in" in capsys.readouterr().out.lower()

    def test_a_bridge_that_cannot_be_built_leaves_local_mode(self, saved, capsys):
        factories = Factories()
        factories.fail.add("codex")
        assert start(cfg(reply_mode="codex", codex_enabled=True), factories, saved) == "local"
        assert runtime.get_service() is None
        assert "local" in capsys.readouterr().out.lower()


@pytest.mark.unit
class TestSwitch:
    def test_switching_replaces_the_bridge_persists_and_notifies(self, saved):
        factories = Factories()
        start(cfg(reply_mode="codex", codex_enabled=True, claude_enabled=True), factories, saved)
        heard = []
        modes.add_listener(lambda mode, enabled: heard.append((mode, tuple(enabled))))
        result = modes.switch("claude")
        assert (result.ok, result.mode, result.previous) == (True, "claude", "codex")
        codex, claude = factories.built
        assert codex.closed == 1 and runtime.get_service() is claude
        assert modes.active_mode() == "claude"
        assert saved == [{"reply_mode": "claude"}]
        assert heard == [("claude", ("local", "codex", "claude"))]

    def test_a_cloud_mode_that_is_not_allowed_is_refused_and_nothing_changes(self, saved):
        factories = Factories()
        start(cfg(), factories, saved)
        result = modes.switch("claude")
        assert (result.ok, result.reason, result.mode) == (False, "not_enabled", "local")
        assert factories.built == [] and saved == [] and modes.active_mode() == "local"

    def test_unknown_modes_are_refused(self, saved):
        start(cfg(claude_enabled=True), Factories(), saved)
        assert modes.switch("gemini").reason == "unknown_mode"

    def test_switching_to_the_active_mode_changes_nothing(self, saved):
        factories = Factories()
        start(cfg(reply_mode="claude", claude_enabled=True), factories, saved)
        result = modes.switch("claude")
        assert (result.ok, result.reason) == (True, "already")
        assert len(factories.built) == 1 and factories.built[0].closed == 0 and saved == []

    def test_going_local_stops_the_bridge(self, saved):
        factories = Factories()
        start(cfg(reply_mode="claude", claude_enabled=True), factories, saved)
        assert modes.switch("local").ok
        assert factories.built[0].closed == 1 and runtime.get_service() is None
        assert saved == [{"reply_mode": "local"}] and modes.active_mode() == "local"

    def test_a_request_in_flight_is_cancelled_by_the_switch(self, saved):
        factories = Factories()
        start(cfg(reply_mode="codex", codex_enabled=True, claude_enabled=True), factories, saved)
        factories.built[0].busy = True
        modes.switch("claude")
        assert factories.built[0].cancelled == ["mode_switch"] and factories.built[0].closed == 1

    def test_a_failed_start_falls_back_to_local_without_persisting(self, saved):
        factories = Factories()
        start(cfg(claude_enabled=True), factories, saved)
        factories.fail.add("claude")
        result = modes.switch("claude")
        assert (result.ok, result.reason, result.mode) == (False, "start_failed", "local")
        assert saved == [] and runtime.get_service() is None

    def test_switching_without_persisting(self, saved):
        start(cfg(claude_enabled=True), Factories(), saved)
        assert modes.switch("claude", persist=False).ok and saved == []

    def test_the_state_lists_the_allowed_modes(self, saved):
        start(cfg(reply_mode="codex", codex_enabled=True), Factories(), saved)
        assert modes.state() == {"mode": "codex", "enabled": ["local", "codex"]}


@pytest.mark.unit
class TestStop:
    def test_stop_closes_the_bridge_and_is_repeatable(self, saved):
        factories = Factories()
        start(cfg(reply_mode="claude", claude_enabled=True), factories, saved)
        modes.stop()
        modes.stop()
        assert factories.built[0].closed == 1 and runtime.get_service() is None
