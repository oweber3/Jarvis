"""Regression tests for the safety hardening batch.

Covers voice-confirmation authorisation gating, tier monotonicity in
``evaluate_safety``, sensitive-location handling for every mutating file
operation, strict process matching for fast-path window commands, and plain
text rendering in the confirmation dialog.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from jarvis.tools.base import Tool
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    SafetyTier,
    evaluate_safety,
    get_confirmation_store,
    set_dialog_callback,
)
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries


class DummyCfg:
    windows_tools_enabled = True
    windows_app_aliases = {}
    voice_debug = False
    tts_rate = 200
    wake_word = "jarvis"
    wake_aliases = []
    wake_fuzzy_ratio = 0.78
    stop_commands = ["stop", "quiet", "shush", "silence", "enough", "shut up"]


@pytest.fixture(autouse=True)
def reset_safety_state():
    store = get_confirmation_store()
    store.clear_pending()
    set_dialog_callback(None)
    yield
    store.clear_pending()
    set_dialog_callback(None)


@pytest.fixture
def mock_home(tmp_path, monkeypatch):
    """Isolate the home directory used by local_files and the safety policy."""
    orig_exp = os.path.expanduser

    def _fake_expand(p):
        if not isinstance(p, str):
            return orig_exp(p)
        if p == "~":
            return str(tmp_path)
        if p.startswith("~/") or p.startswith("~\\"):
            return str(tmp_path / p[2:])
        return orig_exp(p)

    monkeypatch.setattr(os.path, "expanduser", _fake_expand)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Voice confirmation authorisation
# ---------------------------------------------------------------------------

def _listener(*, hot_window: bool, wake: bool = False):
    from jarvis.listening.listener import VoiceListener

    listener = VoiceListener.__new__(VoiceListener)
    listener.cfg = DummyCfg()
    listener.db = SimpleNamespace()
    listener.tts = MagicMock()
    listener.state_manager = MagicMock()
    listener.state_manager.was_speech_during_hot_window.return_value = hot_window
    listener._detected_language = "en"
    listener._wake_timestamp = 1.0 if wake else None
    listener._end_engagement = MagicMock()
    listener.track_tts_start = MagicMock()
    listener.activate_hot_window = MagicMock()
    return listener


def _wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.01)
    return condition()


def _pending_delete(mock_home):
    victim = mock_home / "victim.txt"
    victim.write_text("data", encoding="utf-8")
    outcome = run_tool_with_retries(
        db=None, cfg=DummyCfg(), tool_name="localFiles",
        tool_args={"operation": "delete", "path": "~/victim.txt"},
        system_prompt="", original_prompt="", redacted_text="",
    )
    assert outcome.success is False
    assert get_confirmation_store().has_pending()
    return victim


@pytest.mark.parametrize("utterance", ["yes", "okay", "ok sure", "yeah"])
def test_background_speech_cannot_authorise(mock_home, utterance):
    victim = _pending_delete(mock_home)
    listener = _listener(hot_window=False)

    handled = listener._handle_pending_confirmation(utterance, 10.0, 11.0)

    assert handled is False
    assert victim.exists()
    listener.tts.speak.assert_not_called()
    pending = get_confirmation_store().get_pending()
    assert pending is not None and not pending.authorised


def test_background_no_neither_executes_nor_cancels(mock_home):
    victim = _pending_delete(mock_home)
    listener = _listener(hot_window=False)

    assert listener._handle_pending_confirmation("no", 10.0, 11.0) is False
    assert victim.exists()
    assert get_confirmation_store().has_pending()


def test_hot_window_reply_after_jarvis_asks_authorises(mock_home):
    victim = _pending_delete(mock_home)
    listener = _listener(hot_window=True)

    assert listener._handle_pending_confirmation("yes", 10.0, 11.0) is True

    assert _wait_until(lambda: not victim.exists())
    assert not get_confirmation_store().has_pending()


def test_wake_word_reply_authorises_without_hot_window(mock_home):
    victim = _pending_delete(mock_home)
    listener = _listener(hot_window=False, wake=True)

    assert listener._handle_pending_confirmation("jarvis, yes", 10.0, 11.0) is True
    assert _wait_until(lambda: not victim.exists())


@pytest.mark.parametrize("hot,wake,text", [(True, False, "no"), (False, True, "jarvis no")])
def test_negative_confirmation_cancels(mock_home, hot, wake, text):
    victim = _pending_delete(mock_home)
    listener = _listener(hot_window=hot, wake=wake)

    assert listener._handle_pending_confirmation(text, 10.0, 11.0) is True

    assert victim.exists()
    assert not get_confirmation_store().has_pending()
    assert listener.tts.speak.call_args[0][0] == "Action cancelled."


def test_expired_confirmation_does_not_execute(mock_home):
    victim = _pending_delete(mock_home)
    store = get_confirmation_store()
    store._pending.expires_at = time.time() - 1
    listener = _listener(hot_window=True)

    assert listener._handle_pending_confirmation("yes", 10.0, 11.0) is False
    assert victim.exists()


def test_approval_that_expires_before_execution_does_not_run(mock_home):
    victim = _pending_delete(mock_home)
    store = get_confirmation_store()
    store.handle_voice_response("yes", language="en")
    store._pending.expires_at = time.time() - 1

    assert store.claim_approved() is None
    assert victim.exists()
    assert not store.has_pending()


# ---------------------------------------------------------------------------
# 2. Tier monotonicity
# ---------------------------------------------------------------------------

class _DeclaredTierTool(Tool):
    """A tool that declares a fixed tier and action for whatever it is asked."""

    def __init__(self, tier: SafetyTier, action: str, target: str = "notepad.exe"):
        self._tier, self._action, self._target = tier, action, target

    @property
    def name(self) -> str:
        return "declaredTool"

    @property
    def description(self) -> str:
        return "test"

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    def run(self, args, context):  # pragma: no cover - never executed
        raise AssertionError("not executed")

    def classify_safety(self, args, cfg):
        return ConfirmationRequest(
            tool_name=self.name, tier=self._tier, action=self._action,
            target=self._target, parameters=dict(args or {}),
        )


def _tier(tier, action, target="notepad.exe"):
    tool = _DeclaredTierTool(tier, action, target)
    return evaluate_safety("declaredTool", {}, DummyCfg(), tool=tool, language="en").tier


@pytest.mark.parametrize("action", ["terminate process", "shutdown", "kill", "uninstall"])
def test_declared_deny_is_never_weakened(action):
    assert _tier(SafetyTier.DENY, action) == SafetyTier.DENY


@pytest.mark.parametrize("action", ["terminate process", "kill", "force_close"])
def test_declared_dialog_is_never_weakened_by_process_heuristic(action):
    assert _tier(SafetyTier.CONFIRM_DIALOG, action) == SafetyTier.CONFIRM_DIALOG


def test_declared_dialog_stays_dialog_when_nothing_matches():
    assert _tier(SafetyTier.CONFIRM_DIALOG, "rotate keys") == SafetyTier.CONFIRM_DIALOG


@pytest.mark.parametrize("action", ["shutdown", "restart", "uninstall", 'click "Shut down" in explorer',
                                    'menu "Power > Shut Down" in startmenuexperiencehost'])
def test_declared_voice_escalates_to_dialog(action):
    assert _tier(SafetyTier.CONFIRM_VOICE, action) == SafetyTier.CONFIRM_DIALOG


def test_declared_safe_escalates_normally():
    assert _tier(SafetyTier.SAFE, "shutdown") == SafetyTier.CONFIRM_DIALOG
    assert _tier(SafetyTier.SAFE, "terminate process") == SafetyTier.CONFIRM_VOICE
    assert _tier(SafetyTier.SAFE, "terminate process", "lsass.exe") == SafetyTier.CONFIRM_DIALOG
    assert _tier(SafetyTier.SAFE, "rotate keys") == SafetyTier.SAFE


# ---------------------------------------------------------------------------
# 3. Mutating file operations in sensitive locations
# ---------------------------------------------------------------------------

def _file_tier(operation: str, path: str) -> SafetyTier:
    return evaluate_safety(
        "localFiles", {"operation": operation, "path": path, "content": "x"},
        DummyCfg(), tool=BUILTIN_TOOLS["localFiles"], language="en",
    ).tier


def _sensitive_paths(mock_home, monkeypatch):
    appdata = mock_home / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    system_root = os.environ.get("SystemRoot", "C:\\Windows") if sys.platform == "win32" else "/etc"
    return {
        "ssh": "~/.ssh/authorized_keys",
        "config": "~/.config/tool/settings.json",
        "startup": str(startup / "launcher.bat"),
        "system": os.path.join(system_root, "hosts" if sys.platform != "win32" else "System32/drivers/etc/hosts"),
    }


@pytest.mark.parametrize("operation", ["write", "append", "delete"])
@pytest.mark.parametrize("location", ["ssh", "config", "startup", "system"])
def test_mutations_in_sensitive_locations_need_dialog(mock_home, monkeypatch, operation, location):
    assert _file_tier(operation, _sensitive_paths(mock_home, monkeypatch)[location]) == SafetyTier.CONFIRM_DIALOG


@pytest.mark.parametrize("location", ["ssh", "config", "startup"])
def test_overwrite_of_existing_sensitive_file_needs_dialog(mock_home, monkeypatch, location):
    path = _sensitive_paths(mock_home, monkeypatch)[location]
    target = mock_home / path[2:] if path.startswith("~/") else __import__("pathlib").Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    assert _file_tier("write", path) == SafetyTier.CONFIRM_DIALOG


@pytest.mark.parametrize("location", ["ssh", "config", "startup", "system"])
def test_reading_and_listing_sensitive_locations_stay_safe(mock_home, monkeypatch, location):
    path = _sensitive_paths(mock_home, monkeypatch)[location]
    assert _file_tier("read", path) == SafetyTier.SAFE
    assert _file_tier("list", path) == SafetyTier.SAFE


def test_ordinary_user_file_creation_and_append_stay_safe(mock_home):
    (mock_home / "notes.txt").write_text("hello", encoding="utf-8")
    assert _file_tier("write", "~/fresh.txt") == SafetyTier.SAFE
    assert _file_tier("write", "~/Documents/new/report.txt") == SafetyTier.SAFE
    assert _file_tier("append", "~/notes.txt") == SafetyTier.SAFE


def test_ordinary_delete_and_overwrite_still_use_voice_confirmation(mock_home):
    (mock_home / "notes.txt").write_text("hello", encoding="utf-8")
    assert _file_tier("delete", "~/notes.txt") == SafetyTier.CONFIRM_VOICE
    assert _file_tier("write", "~/notes.txt") == SafetyTier.CONFIRM_VOICE


def test_sensitive_append_is_blocked_without_a_desktop_dialog(mock_home):
    outcome = run_tool_with_retries(
        db=None, cfg=DummyCfg(), tool_name="localFiles",
        tool_args={"operation": "append", "path": "~/.ssh/authorized_keys", "content": "ssh-rsa AAA"},
        system_prompt="", original_prompt="", redacted_text="",
    )
    assert outcome.success is False
    assert not (mock_home / ".ssh" / "authorized_keys").exists()


# ---------------------------------------------------------------------------
# 4. Fast-path window targeting
# ---------------------------------------------------------------------------

def _windows():
    from jarvis.platform.windows.windows_mgmt import Window

    return [Window(101, "Code review - Google Chrome", "chrome", 1)]


def test_process_only_resolution_ignores_title_matches():
    from jarvis.platform.windows.windows_mgmt import resolve_window

    with pytest.raises(ValueError):
        resolve_window("code", _windows(), process_only=True)


def test_manual_resolution_keeps_title_fallback():
    from jarvis.platform.windows.windows_mgmt import resolve_window

    assert resolve_window("code", _windows()).hwnd == 101


def test_process_only_resolution_still_matches_the_process():
    from jarvis.platform.windows.windows_mgmt import Window, resolve_window

    windows = _windows() + [Window(202, "main.py - Visual Studio Code", "Code", 2)]
    assert resolve_window("code", windows, process_only=True).hwnd == 202


@pytest.mark.parametrize("tool_name,action", [
    ("appControl", "close"), ("appControl", "focus"),
    ("windowControl", "minimise"), ("windowControl", "maximise"), ("windowControl", "restore"),
])
def test_fast_window_command_never_touches_unrelated_window(monkeypatch, tool_name, action):
    from jarvis.platform.windows import windows_mgmt as wm

    touched = []
    monkeypatch.setattr(wm, "list_windows", _windows)
    monkeypatch.setattr(wm, "_close_window", touched.append)
    monkeypatch.setattr(wm, "_focus_window", lambda hwnd: touched.append(hwnd) or True)
    monkeypatch.setattr(wm, "_show_window", lambda hwnd, value: touched.append(hwnd))

    tool = BUILTIN_TOOLS[tool_name]
    cfg = SimpleNamespace(windows_tools_enabled=True, windows_app_aliases={})
    result = tool.run(
        {"action": action, "target": "code", "match": "process"},
        SimpleNamespace(cfg=cfg, user_print=lambda *_: None),
    )

    assert result.success is False
    assert touched == []


def test_manual_window_command_keeps_title_fallback(monkeypatch):
    from jarvis.platform.windows import windows_mgmt as wm

    closed = []
    monkeypatch.setattr(wm, "list_windows", _windows)
    monkeypatch.setattr(wm, "_close_window", closed.append)
    cfg = SimpleNamespace(windows_tools_enabled=True, windows_app_aliases={})
    result = BUILTIN_TOOLS["appControl"].run(
        {"action": "close", "target": "code"}, SimpleNamespace(cfg=cfg, user_print=lambda *_: None))
    assert result.success is True and closed == [101]


def test_window_tools_reject_unknown_match_modes():
    cfg = SimpleNamespace(windows_tools_enabled=True, windows_app_aliases={})
    result = BUILTIN_TOOLS["appControl"].run(
        {"action": "close", "target": "code", "match": "title"},
        SimpleNamespace(cfg=cfg, user_print=lambda *_: None))
    assert result.success is False


@pytest.mark.parametrize("phrase,expects_process_match", [
    ("close code", True), ("switch to code", True), ("minimise code", True),
    ("open code", False),
])
def test_fast_matcher_requests_process_only_for_window_actions(phrase, expects_process_match):
    from jarvis.fastpath.matcher import FastTarget, match

    targets = (FastTarget(names=("visual studio code", "code"), open_target="Visual Studio Code",
                          window_target="Code", display="Visual Studio Code"),)
    result = match(phrase, "en", targets=targets,
                   available_tools={"appControl", "windowControl"})
    assert result is not None
    assert (result.args.get("match") == "process") is expects_process_match


# ---------------------------------------------------------------------------
# 5. Confirmation dialog renders plain text
# ---------------------------------------------------------------------------

def test_dialog_renders_dynamic_values_as_plain_text(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QLabel
    from desktop_app.confirmation_dialog import ActionConfirmationDialog

    action = "<b>delete</b> <img src=x onerror=boom>"
    target = "<a href='http://evil.example'>C:\\important</a>"
    consequence = "<h1>Gone</h1> &amp; more"
    dlg = ActionConfirmationDialog(action=action, target=target, consequence=consequence)

    labels = [label for label in dlg.findChildren(QLabel)]
    dynamic = [label for label in labels if any(v in label.text() for v in (action, target, consequence))]
    assert len(dynamic) == 3
    for label in dynamic:
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert not label.openExternalLinks()


# ---------------------------------------------------------------------------
# Debug logs name the tool, never the control, file or window it acts on
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_confirmation_logs_carry_no_control_names_or_paths(monkeypatch, capsys):
    from jarvis import debug
    from jarvis.tools.confirmation import ConfirmationStore
    monkeypatch.setattr(debug, "_is_debug_enabled", lambda: True)
    store = ConfirmationStore()
    request = ConfirmationRequest(tool_name="uiControl", tier=SafetyTier.CONFIRM_VOICE,
                                  action='click "Delete forever" in outlook', target=r"C:\Users\you\payroll.xlsx",
                                  parameters={})
    store.set_pending(request)
    store.handle_voice_response("yes")
    store.set_pending(request)
    store.handle_voice_response("no")
    store.set_pending(request)
    store.clear_pending()
    err = capsys.readouterr().err
    assert "uiControl" in err
    assert "Delete forever" not in err and "payroll" not in err and "outlook" not in err
