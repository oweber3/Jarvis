"""Tests for centralized tool action confirmation and safety policy."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from jarvis.tools.base import Tool, ToolContext
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    ConfirmationStore,
    SafetyTier,
    VoiceResponseStatus,
    evaluate_safety,
    get_confirmation_store,
    get_dialog_callback,
    is_critical_process,
    is_system_or_important_location,
    set_dialog_callback,
)
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult


class DummyDB:
    pass


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
    """Ensure confirmation store and dialog callback are clean before and after each test."""
    store = get_confirmation_store()
    store.clear_pending()
    set_dialog_callback(None)
    yield
    store.clear_pending()
    set_dialog_callback(None)


@pytest.fixture
def mock_home(tmp_path, monkeypatch):
    """Safely isolate home directory to tmp_path across local_files and confirmation."""
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
    import jarvis.tools.builtin.local_files as lf_mod
    monkeypatch.setattr(lf_mod.os.path, "expanduser", _fake_expand)
    import jarvis.tools.confirmation as conf_mod
    monkeypatch.setattr(conf_mod.os.path, "expanduser", _fake_expand)
    return tmp_path


def test_safe_actions_execute_directly(mock_home):
    """SAFE actions must execute directly without confirmation prompts."""
    db = DummyDB()
    cfg = DummyCfg()
    target_file = mock_home / "safe_test.txt"

    result = run_tool_with_retries(
        db=db,
        cfg=cfg,
        tool_name="localFiles",
        tool_args={"operation": "write", "path": "~/safe_test.txt", "content": "hello safe world"},
        system_prompt="",
        original_prompt="",
        redacted_text="",
    )

    assert result.success is True
    assert target_file.read_text(encoding="utf-8") == "hello safe world"
    assert not get_confirmation_store().has_pending()


def test_localfiles_delete_requires_confirmation(mock_home):
    """Deleting an existing file must require CONFIRM_VOICE and not execute immediately."""
    db = DummyDB()
    cfg = DummyCfg()
    target_file = mock_home / "delete_me.txt"
    target_file.write_text("important data", encoding="utf-8")

    result = run_tool_with_retries(
        db=db,
        cfg=cfg,
        tool_name="localFiles",
        tool_args={"operation": "delete", "path": "~/delete_me.txt"},
        system_prompt="",
        original_prompt="",
        redacted_text="",
    )

    assert result.success is False
    assert target_file.exists()
    assert "confirmation" in (result.reply_text or "").lower()
    assert "delete" in (result.reply_text or "").lower()

    store = get_confirmation_store()
    assert store.has_pending()
    pending = store.get_pending()
    assert pending is not None
    assert pending.request.tier == SafetyTier.CONFIRM_VOICE
    assert pending.request.action == "delete file"


def test_localfiles_overwrite_requires_confirmation(mock_home):
    """Overwriting an existing file must require CONFIRM_VOICE, while new file is SAFE."""
    db = DummyDB()
    cfg = DummyCfg()
    target_file = mock_home / "overwrite_me.txt"
    target_file.write_text("initial content", encoding="utf-8")

    result = run_tool_with_retries(
        db=db,
        cfg=cfg,
        tool_name="localFiles",
        tool_args={"operation": "write", "path": "~/overwrite_me.txt", "content": "new content"},
        system_prompt="",
        original_prompt="",
        redacted_text="",
    )

    assert result.success is False
    assert target_file.read_text(encoding="utf-8") == "initial content"
    assert "confirmation" in (result.reply_text or "").lower()
    assert "overwrite" in (result.reply_text or "").lower()

    store = get_confirmation_store()
    assert store.has_pending()
    pending = store.get_pending()
    assert pending is not None
    assert pending.request.tier == SafetyTier.CONFIRM_VOICE
    assert pending.request.action == "overwrite file"


def test_voice_confirmed_destructive_action(mock_home):
    """Spoken affirmative confirmation must authorise and execute the pending action."""
    db = DummyDB()
    cfg = DummyCfg()
    target_file = mock_home / "confirm_delete.txt"
    target_file.write_text("data to delete", encoding="utf-8")

    init_res = run_tool_with_retries(
        db=db,
        cfg=cfg,
        tool_name="localFiles",
        tool_args={"operation": "delete", "path": "~/confirm_delete.txt"},
        system_prompt="",
        original_prompt="",
        redacted_text="",
    )
    assert init_res.success is False

    store = get_confirmation_store()
    assert store.has_pending()

    # User speaks affirmative response
    status = store.handle_voice_response("yes", language="en")
    assert status == VoiceResponseStatus.AFFIRMATIVE

    # Execute pending action
    exec_res = store.run_approved(store.claim_approved(), db=db, cfg=cfg)
    assert exec_res.success is True
    assert not target_file.exists()
    assert not store.has_pending()


def test_rejected_voice_confirmation(mock_home):
    """Spoken negative response must cancel the pending action without executing it."""
    db = DummyDB()
    cfg = DummyCfg()
    target_file = mock_home / "keep_me.txt"
    target_file.write_text("do not delete", encoding="utf-8")

    init_res = run_tool_with_retries(
        db=db,
        cfg=cfg,
        tool_name="localFiles",
        tool_args={"operation": "delete", "path": "~/keep_me.txt"},
        system_prompt="",
        original_prompt="",
        redacted_text="",
    )
    assert init_res.success is False

    store = get_confirmation_store()
    assert store.has_pending()

    # User says no
    status = store.handle_voice_response("no", language="en")
    assert status == VoiceResponseStatus.NEGATIVE
    assert not store.has_pending()
    assert target_file.exists()


def test_expired_confirmation():
    """Pending confirmation must expire after timeout and not authorize execution."""
    store = get_confirmation_store()
    req = ConfirmationRequest(
        tool_name="localFiles",
        tier=SafetyTier.CONFIRM_VOICE,
        action="delete file",
        target="foo.txt",
        parameters={"operation": "delete", "path": "foo.txt"},
    )
    store.set_pending(req, timeout_sec=0.01)
    time.sleep(0.05)

    status = store.handle_voice_response("yes", language="en")
    assert status == VoiceResponseStatus.EXPIRED
    assert not store.has_pending()


def test_unrelated_speech_does_not_confirm():
    """Unrelated speech must abandon/cancel pending confirmation without approving it."""
    store = get_confirmation_store()
    req = ConfirmationRequest(
        tool_name="localFiles",
        tier=SafetyTier.CONFIRM_VOICE,
        action="delete file",
        target="foo.txt",
        parameters={"operation": "delete", "path": "foo.txt"},
    )
    store.set_pending(req, timeout_sec=30.0)

    status = store.handle_voice_response("what is the weather in London today?", language="en")
    assert status == VoiceResponseStatus.UNRELATED
    assert not store.has_pending()


def test_confirmation_bound_to_exact_action():
    """Authorization for action A must not authorize action B or modified parameters."""
    store = get_confirmation_store()
    req = ConfirmationRequest(
        tool_name="localFiles",
        tier=SafetyTier.CONFIRM_VOICE,
        action="delete file",
        target="file1.txt",
        parameters={"operation": "delete", "path": "file1.txt"},
    )
    store.set_pending(req)
    status = store.handle_voice_response("yes", language="en")
    assert status == VoiceResponseStatus.AFFIRMATIVE

    # Trying to claim authorization for file2.txt must fail
    assert not store.is_authorized(
        tool_name="localFiles",
        action="delete file",
        target="file2.txt",
        parameters={"operation": "delete", "path": "file2.txt"},
    )

    # Trying to claim authorization with different tool must fail
    assert not store.is_authorized(
        tool_name="otherTool",
        action="delete file",
        target="file1.txt",
        parameters={"operation": "delete", "path": "file1.txt"},
    )

    # Exact match succeeds
    assert store.is_authorized(
        tool_name="localFiles",
        action="delete file",
        target="file1.txt",
        parameters={"operation": "delete", "path": "file1.txt"},
    )


def test_high_risk_action_requires_dialog():
    """High risk actions require desktop confirmation dialog; voice alone cannot authorize."""
    class HighRiskTool(Tool):
        name = "systemPower"
        description = "Power control"
        inputSchema = {"type": "object", "properties": {"action": {"type": "string"}}}

        def classify_safety(self, args, cfg):
            return ConfirmationRequest(
                tool_name=self.name,
                tier=SafetyTier.CONFIRM_DIALOG,
                action="shutdown",
                target="local system",
                parameters=dict(args or {}),
                consequence="System will power off immediately.",
            )

        def run(self, args, context):
            return ToolExecutionResult(success=True, reply_text="System powering off.")

    tool = HighRiskTool()
    BUILTIN_TOOLS["systemPower"] = tool
    try:
        db = DummyDB()
        cfg = DummyCfg()

        # In headless mode (no dialog callback registered), must be refused
        headless_res = run_tool_with_retries(
            db=db,
            cfg=cfg,
            tool_name="systemPower",
            tool_args={"action": "shutdown"},
            system_prompt="",
            original_prompt="",
            redacted_text="",
        )
        assert headless_res.success is False
        assert "confirmation dialog" in (headless_res.error_message or "").lower()

        # Voice response should NOT be accepted (nothing pending in voice store)
        assert not get_confirmation_store().has_pending()

        # With a desktop dialog: the request returns at once and the answer
        # runs the action on a worker thread.
        import threading
        from jarvis.tools.confirmation import set_result_handler
        delivered, done = [], threading.Event()
        set_result_handler(lambda reply, ok: (delivered.append((reply, ok)), done.set()))
        mock_dialog_cb = MagicMock(return_value=MagicMock())
        set_dialog_callback(mock_dialog_cb)

        pending_res = run_tool_with_retries(
            db=db,
            cfg=cfg,
            tool_name="systemPower",
            tool_args={"action": "shutdown"},
            system_prompt="",
            original_prompt="",
            redacted_text="",
        )
        assert mock_dialog_cb.called
        assert pending_res.success is False
        assert "confirm" in (pending_res.reply_text or "").lower()
        assert not done.is_set()

        _request, resolve = mock_dialog_cb.call_args[0]
        resolve(True)
        assert done.wait(5)
        assert delivered == [("System powering off.", True)]
    finally:
        BUILTIN_TOOLS.pop("systemPower", None)


def test_canceling_dialog_prevents_execution():
    """Cancelling or closing the confirmation dialog must prevent execution."""
    class DestructiveTool(Tool):
        name = "uninstallApp"
        description = "Uninstall software"
        inputSchema = {"type": "object", "properties": {"target": {"type": "string"}}}

        def classify_safety(self, args, cfg):
            return ConfirmationRequest(
                tool_name=self.name,
                tier=SafetyTier.CONFIRM_DIALOG,
                action="uninstall",
                target=args.get("target", ""),
                parameters=dict(args or {}),
            )

        def run(self, args, context):
            tool_ran.append(True)
            return ToolExecutionResult(success=True, reply_text="Uninstalled.")

    tool_ran = []
    tool = DestructiveTool()
    BUILTIN_TOOLS["uninstallApp"] = tool
    try:
        db = DummyDB()
        cfg = DummyCfg()
        mock_dialog_cb = MagicMock(return_value=MagicMock())
        set_dialog_callback(mock_dialog_cb)

        res = run_tool_with_retries(
            db=db,
            cfg=cfg,
            tool_name="uninstallApp",
            tool_args={"target": "MalwareApp"},
            system_prompt="",
            original_prompt="",
            redacted_text="",
        )
        assert mock_dialog_cb.called
        assert res.success is False

        _request, resolve = mock_dialog_cb.call_args[0]
        resolve(False)  # Cancel (or closing the dialog)
        assert not get_confirmation_store().has_pending()
        assert tool_ran == []
    finally:
        BUILTIN_TOOLS.pop("uninstallApp", None)


def test_prohibited_actions_are_denied():
    """Actions classified as DENY must be refused unconditionally."""
    req = evaluate_safety(
        tool_name="diskUtility",
        tool_args={"action": "format_drive", "target": "C:"},
        cfg=DummyCfg(),
    )
    assert req.tier == SafetyTier.DENY


def test_critical_process_termination_requires_dialog():
    """Terminating critical system processes must require CONFIRM_DIALOG, not CONFIRM_VOICE."""
    assert is_critical_process("explorer.exe")
    assert is_critical_process("svchost.exe")

    req = evaluate_safety(
        tool_name="processControl",
        tool_args={"action": "terminate", "target": "explorer.exe"},
        cfg=DummyCfg(),
    )
    assert req.tier == SafetyTier.CONFIRM_DIALOG


def test_unsupported_language_escalates_to_dialog(mock_home):
    """When detected language has no phrase table, CONFIRM_VOICE escalates to CONFIRM_DIALOG."""
    target_file = mock_home / "foreign_delete.txt"
    target_file.write_text("content", encoding="utf-8")

    req = evaluate_safety(
        tool_name="localFiles",
        tool_args={"operation": "delete", "path": "~/foreign_delete.txt"},
        cfg=DummyCfg(),
        language="xx",
    )
    assert req.tier == SafetyTier.CONFIRM_DIALOG


def test_listener_voice_confirmation_affirmative():
    """VoiceListener executes pending action and speaks result upon affirmative utterance."""
    from jarvis.listening.listener import VoiceListener
    listener = VoiceListener.__new__(VoiceListener)
    listener.cfg = DummyCfg()
    listener.db = DummyDB()
    listener.tts = MagicMock()
    listener.state_manager = MagicMock()
    # Jarvis has just asked for confirmation, so the hot window is open.
    listener.state_manager.was_speech_during_hot_window.return_value = True
    listener.echo_detector = MagicMock()
    listener.echo_detector._tts_start_time = 0.0
    listener.echo_detector._last_tts_text = ""
    listener.echo_detector._last_tts_finish_time = 0.0
    listener.echo_detector.echo_tolerance = 0.5
    listener._is_engaged = lambda: False
    listener._start_engagement = MagicMock()
    listener._set_face_state_listening = MagicMock()
    listener._end_engagement = MagicMock()
    listener.track_tts_start = MagicMock()
    listener.activate_hot_window = MagicMock()
    listener._detected_language = "en"

    store = get_confirmation_store()
    req = ConfirmationRequest(
        tool_name="testTool",
        tier=SafetyTier.CONFIRM_VOICE,
        action="delete file",
        target="foo.txt",
        parameters={},
    )
    store.set_pending(req)
    with patch.object(store, "execute_pending_async", return_value=True) as scheduled:
        listener._process_transcript("yes", captured_during_tts=False, captured_tts_start_time=0.0)

    scheduled.assert_called_once()
    listener.tts.speak.assert_not_called()  # the result is spoken when the worker finishes


def test_listener_voice_confirmation_negative():
    """VoiceListener cancels pending action upon negative utterance."""
    from jarvis.listening.listener import VoiceListener
    listener = VoiceListener.__new__(VoiceListener)
    listener.cfg = DummyCfg()
    listener.db = DummyDB()
    listener.tts = MagicMock()
    listener.state_manager = MagicMock()
    # Jarvis has just asked for confirmation, so the hot window is open.
    listener.state_manager.was_speech_during_hot_window.return_value = True
    listener.echo_detector = MagicMock()
    listener.echo_detector._tts_start_time = 0.0
    listener.echo_detector._last_tts_text = ""
    listener.echo_detector._last_tts_finish_time = 0.0
    listener.echo_detector.echo_tolerance = 0.5
    listener._is_engaged = lambda: False
    listener._start_engagement = MagicMock()
    listener._set_face_state_listening = MagicMock()
    listener._end_engagement = MagicMock()
    listener.track_tts_start = MagicMock()
    listener.activate_hot_window = MagicMock()
    listener._detected_language = "en"

    store = get_confirmation_store()
    req = ConfirmationRequest(
        tool_name="testTool",
        tier=SafetyTier.CONFIRM_VOICE,
        action="delete file",
        target="foo.txt",
        parameters={},
    )
    store.set_pending(req)
    listener._process_transcript("no", captured_during_tts=False, captured_tts_start_time=0.0)

    assert listener.tts.speak.call_args[0][0] == "Action cancelled."
    assert not store.has_pending()


def test_desktop_action_confirmation_dialog_accept(qapp):
    """ActionConfirmationDialog confirms when user clicks Confirm."""
    from desktop_app.confirmation_dialog import ActionConfirmationDialog
    dlg = ActionConfirmationDialog(action="shutdown", target="system", consequence="PC will shut down.")
    assert dlg.action == "shutdown"
    assert dlg.target == "system"
    assert dlg.consequence == "PC will shut down."
    dlg.confirm_button.click()
    assert dlg.result() == 1


def test_desktop_action_confirmation_dialog_cancel(qapp):
    """ActionConfirmationDialog cancels when user clicks Cancel."""
    from desktop_app.confirmation_dialog import ActionConfirmationDialog
    dlg = ActionConfirmationDialog(action="shutdown", target="system")
    dlg.cancel_button.click()
    assert dlg.result() == 0

