"""The update check asks GitHub over the network, so it never runs on the Qt main thread.

Offline or behind a captive portal the request waits for its timeout, and on the main thread that freezes
the tray and every window ("Not Responding").
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


def _tray():
    return SimpleNamespace(_update_signals=SimpleNamespace(checked=Mock()), _update_check_running=False,
                           _update_dialog_requested=False)


def _wait_for(mock, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline and not mock.called:
        time.sleep(0.01)
    return mock.called


def test_the_check_runs_off_the_main_thread_and_returns_at_once(monkeypatch):
    from desktop_app import app as app_module
    import desktop_app.updater as updater

    release = threading.Event()
    checked_on = {}

    def slow_check():
        checked_on["thread"] = threading.current_thread().name
        release.wait(5)
        return updater.UpdateStatus(update_available=False, current_version="1", current_channel="stable",
                                    latest_release=None)

    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "check_for_updates", slow_check)
    tray = _tray()

    started = time.monotonic()
    app_module.JarvisSystemTray.check_for_updates(tray, show_no_update_dialog=True)
    assert time.monotonic() - started < 1.0, "the caller waited for the network"

    release.set()
    assert _wait_for(tray._update_signals.checked.emit)
    status, show_dialog = tray._update_signals.checked.emit.call_args.args
    assert status.update_available is False and show_dialog is True
    assert checked_on["thread"] != threading.current_thread().name


def test_a_failed_check_is_delivered_as_an_error(monkeypatch):
    from desktop_app import app as app_module
    import desktop_app.updater as updater

    def failing_check():
        raise OSError("network down")

    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "check_for_updates", failing_check)
    tray = _tray()

    app_module.JarvisSystemTray.check_for_updates(tray)

    assert _wait_for(tray._update_signals.checked.emit)
    status, show_dialog = tray._update_signals.checked.emit.call_args.args
    assert status.error and show_dialog is False


def test_a_second_check_while_one_runs_is_skipped(monkeypatch):
    from desktop_app import app as app_module
    import desktop_app.updater as updater

    calls = []
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater, "check_for_updates", lambda: calls.append(1))
    tray = _tray()
    tray._update_check_running = True

    app_module.JarvisSystemTray.check_for_updates(tray)

    time.sleep(0.1)
    assert calls == []


class TestStartUpCheckIsOptIn:
    """Nothing contacts GitHub at start-up unless the user turned the start-up check on."""

    def test_off_when_the_config_does_not_mention_it(self, tmp_path, monkeypatch):
        from desktop_app.updater import startup_check_enabled
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "config.json"))
        assert startup_check_enabled() is False

    def test_on_when_the_user_turned_it_on(self, tmp_path, monkeypatch):
        import json
        from desktop_app.updater import startup_check_enabled
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"update_check_on_startup": True}), encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))
        assert startup_check_enabled() is True

    def test_the_settings_window_offers_the_switch(self):
        from desktop_app.settings_window import FIELD_METADATA
        assert any(field.key == "update_check_on_startup" for field in FIELD_METADATA)


def test_asking_while_the_start_up_check_runs_still_answers(monkeypatch):
    """A Check for Updates click during the silent start-up check gets its dialog when that check ends."""
    from desktop_app import app as app_module
    import desktop_app.updater as updater

    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    tray = _tray()
    tray._update_check_running = True

    app_module.JarvisSystemTray.check_for_updates(tray, show_no_update_dialog=True)

    shown = []
    monkeypatch.setattr("desktop_app.update_dialog.show_no_update_dialog", lambda version: shown.append(version))
    status = updater.UpdateStatus(update_available=False, current_version="1.2.3", current_channel="stable",
                                  latest_release=None)
    app_module.JarvisSystemTray._on_update_checked(tray, status, False)

    assert shown == ["1.2.3"]
    assert tray._update_check_running is False
