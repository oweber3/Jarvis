"""Confirmation must never hold the listener or query path.

Dialog confirmation is non-blocking and bound to a pending action with its own
expiry. Confirmed tools (voice or dialog) run on a worker thread and report
through the registered result handler.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import jarvis.tools.confirmation as confirmation
from jarvis.tools.base import Tool
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    SafetyTier,
    VoiceResponseStatus,
    get_confirmation_store,
    set_dialog_callback,
    set_result_handler,
)
from jarvis.tools.registry import BUILTIN_TOOLS, run_tool_with_retries
from jarvis.tools.types import ToolExecutionResult


class Cfg:
    windows_tools_enabled = True
    windows_app_aliases = {}
    voice_debug = False
    wake_word = "jarvis"
    wake_aliases = []
    wake_fuzzy_ratio = 0.78
    tts_rate = 200


class RecordingTool(Tool):
    """Destructive tool whose run can be held open and observed."""

    def __init__(self, name: str, tier: SafetyTier):
        self._name, self._tier = name, tier
        self.runs: list[int] = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return "test"

    @property
    def inputSchema(self):
        return {"type": "object", "properties": {"target": {"type": "string"}}}

    def classify_safety(self, args, cfg):
        return ConfirmationRequest(
            tool_name=self._name, tier=self._tier, action="uninstall"
            if self._tier == SafetyTier.CONFIRM_DIALOG else "terminate process",
            target=str((args or {}).get("target", "thing")), parameters=dict(args or {}),
        )

    def run(self, args, context):
        self.runs.append(threading.get_ident())
        self.started.set()
        self.release.wait(5)
        return ToolExecutionResult(success=True, reply_text="Done.")


class FakeHandle:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class FakeDialogs:
    """Stands in for the desktop layer: records requests, never blocks."""

    def __init__(self):
        self.shown: list[tuple] = []
        self.handles: list[FakeHandle] = []

    def __call__(self, request, resolve):
        handle = FakeHandle()
        self.shown.append((request, resolve))
        self.handles.append(handle)
        return handle


@pytest.fixture(autouse=True)
def clean_state():
    store = get_confirmation_store()
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)
    yield
    store.clear_pending()
    set_dialog_callback(None)
    set_result_handler(None)


@pytest.fixture
def dialogs():
    fake = FakeDialogs()
    set_dialog_callback(fake)
    return fake


@pytest.fixture
def results():
    """Collects delivered results and lets a test wait for the next one."""
    box = SimpleNamespace(items=[], event=threading.Event())

    def handler(reply, success):
        box.items.append((reply, success))
        box.event.set()

    set_result_handler(handler)
    return box


@pytest.fixture
def dialog_tool():
    tool = RecordingTool("dialogTool", SafetyTier.CONFIRM_DIALOG)
    BUILTIN_TOOLS["dialogTool"] = tool
    yield tool
    BUILTIN_TOOLS.pop("dialogTool", None)


@pytest.fixture
def voice_tool():
    tool = RecordingTool("voiceTool", SafetyTier.CONFIRM_VOICE)
    BUILTIN_TOOLS["voiceTool"] = tool
    yield tool
    BUILTIN_TOOLS.pop("voiceTool", None)


def _call(name, target="thing"):
    return run_tool_with_retries(
        db=None, cfg=Cfg(), tool_name=name, tool_args={"target": target},
        system_prompt="", original_prompt="", redacted_text="",
    )


# ---------------------------------------------------------------------------
# Dialog confirmation does not block
# ---------------------------------------------------------------------------

def test_opening_a_dialog_does_not_block_the_caller(dialogs, dialog_tool):
    started = time.monotonic()
    result = _call("dialogTool")

    assert time.monotonic() - started < 1.0
    assert result.success is False and result.reply_text
    assert dialog_tool.runs == []
    assert len(dialogs.shown) == 1


def test_query_lock_is_free_while_a_dialog_is_pending(dialogs, dialog_tool):
    from jarvis import daemon

    with daemon.query_lock():
        _call("dialogTool")
    # Taking the lock again (non-blocking) proves nothing kept hold of it.
    assert daemon._chat_query_lock.acquire(blocking=False)
    daemon._chat_query_lock.release()


def test_other_queries_proceed_while_a_dialog_is_pending(dialogs, dialog_tool):
    class Safe(RecordingTool):
        def classify_safety(self, args, cfg):
            return ConfirmationRequest(self._name, SafetyTier.SAFE, "read", "", {})

    safe = Safe("safeTool", SafetyTier.SAFE)
    BUILTIN_TOOLS["safeTool"] = safe
    try:
        _call("dialogTool")
        outcome = _call("safeTool")
    finally:
        BUILTIN_TOOLS.pop("safeTool", None)
    assert outcome.success is True and len(safe.runs) == 1


def test_chat_text_does_not_cancel_a_pending_dialog(dialogs, dialog_tool):
    _call("dialogTool")
    store = get_confirmation_store()
    assert store.has_pending() and not store.has_pending_voice()
    # Speech or typed text can neither approve nor abandon a dialog request.
    assert store.handle_voice_response("yes", language="en") == VoiceResponseStatus.UNRELATED
    assert store.has_pending()
    assert dialog_tool.runs == []


# ---------------------------------------------------------------------------
# Dialog outcomes
# ---------------------------------------------------------------------------

def test_confirm_executes_exactly_once_off_the_calling_thread(dialogs, dialog_tool, results):
    _call("dialogTool")
    _, resolve = dialogs.shown[0]

    resolve(True)
    resolve(True)  # a second click or duplicate callback

    assert results.event.wait(5)
    time.sleep(0.1)
    assert len(dialog_tool.runs) == 1
    assert dialog_tool.runs[0] != threading.get_ident()
    assert results.items == [("Done.", True)]
    assert not get_confirmation_store().has_pending()


def test_cancel_never_executes(dialogs, dialog_tool, results):
    _call("dialogTool")
    _, resolve = dialogs.shown[0]

    resolve(False)
    resolve(True)  # a later approval of a cancelled request is ignored

    time.sleep(0.2)
    assert dialog_tool.runs == []
    assert dialogs.handles[0].closed.is_set()
    assert results.items == []


def test_expiry_invalidates_the_request_and_closes_the_dialog(monkeypatch, dialogs, dialog_tool, results):
    monkeypatch.setattr(confirmation, "DIALOG_TIMEOUT_SEC", 0.05)
    _call("dialogTool")
    _, resolve = dialogs.shown[0]

    assert dialogs.handles[0].closed.wait(2)  # no stale window left open
    assert not get_confirmation_store().has_pending()

    resolve(True)  # a late Confirm click
    time.sleep(0.2)
    assert dialog_tool.runs == []
    assert results.items == []


def test_a_new_request_replaces_and_closes_the_previous_dialog(dialogs, dialog_tool):
    _call("dialogTool", "first")
    _, stale_resolve = dialogs.shown[0]
    _call("dialogTool", "second")

    assert dialogs.handles[0].closed.is_set()
    stale_resolve(True)
    time.sleep(0.2)
    assert dialog_tool.runs == []


def test_shutdown_closes_pending_dialogs(dialogs, dialog_tool):
    _call("dialogTool")
    set_dialog_callback(None)  # desktop shutting down

    assert dialogs.handles[0].closed.is_set()
    assert not get_confirmation_store().has_pending()


def test_dialog_approval_is_bound_to_the_exact_action(dialogs, dialog_tool, results):
    _call("dialogTool", "alpha")
    store = get_confirmation_store()
    req = store.get_pending().request
    _, resolve = dialogs.shown[0]
    resolve(True)
    assert results.event.wait(5)
    # The approval was single use: it cannot authorise anything afterwards.
    assert not store.is_authorised(req.tool_name, req.action, req.target, req.parameters)
    assert len(dialog_tool.runs) == 1


def test_changed_arguments_are_not_covered_by_an_approval(dialogs, dialog_tool):
    _call("dialogTool", "alpha")
    store = get_confirmation_store()
    req = store.get_pending().request
    pending = store.get_pending()
    pending.authorised = True
    pending.authorised_at = time.time()
    assert store.is_authorised(req.tool_name, req.action, req.target, req.parameters)
    assert not store.is_authorised(req.tool_name, req.action, "beta", {"target": "beta"})


# ---------------------------------------------------------------------------
# Voice confirmation executes off the listener thread
# ---------------------------------------------------------------------------

def _listener():
    from jarvis.listening.listener import VoiceListener

    listener = VoiceListener.__new__(VoiceListener)
    listener.cfg = Cfg()
    listener.db = SimpleNamespace()
    listener.tts = MagicMock()
    listener.state_manager = MagicMock()
    listener.state_manager.was_speech_during_hot_window.return_value = True
    listener._last_detected_language = "en"
    listener._wake_timestamp = None
    listener._end_engagement = MagicMock()
    listener.track_tts_start = MagicMock()
    listener.activate_hot_window = MagicMock()
    return listener


def test_voice_approval_schedules_execution_instead_of_running_inline(voice_tool, results):
    voice_tool.release.clear()  # the tool will take a while
    listener = _listener()
    assert _call("voiceTool").success is False

    started = time.monotonic()
    handled = listener._handle_pending_confirmation("yes", 1.0, 2.0)
    elapsed = time.monotonic() - started

    assert handled is True and elapsed < 1.0
    assert voice_tool.started.wait(2)
    assert voice_tool.runs[0] != threading.get_ident()
    assert results.items == []  # still running; the listener already returned

    voice_tool.release.set()
    assert results.event.wait(5)
    assert results.items == [("Done.", True)]


def test_listener_keeps_processing_while_the_confirmed_tool_runs(voice_tool, results):
    from jarvis import daemon

    voice_tool.release.clear()
    listener = _listener()
    _call("voiceTool")
    listener._handle_pending_confirmation("yes", 1.0, 2.0)
    assert voice_tool.started.wait(2)

    # Further speech is handled immediately and the query lock is free.
    assert listener._handle_pending_confirmation("what time is it", 3.0, 4.0) is False
    assert daemon._chat_query_lock.acquire(blocking=False)
    daemon._chat_query_lock.release()
    voice_tool.release.set()


def test_double_affirmative_executes_once(voice_tool, results):
    listener = _listener()
    _call("voiceTool")

    first = listener._handle_pending_confirmation("yes", 1.0, 2.0)
    second = listener._handle_pending_confirmation("yes", 2.0, 3.0)

    assert first is True and second is False
    assert results.event.wait(5)
    time.sleep(0.1)
    assert len(voice_tool.runs) == 1


def test_approval_survives_expiry_between_approval_and_scheduling(voice_tool, results):
    store = get_confirmation_store()
    _call("voiceTool")
    store._pending.expires_at = time.time() + 0.2
    assert store.handle_voice_response("yes", language="en") == VoiceResponseStatus.AFFIRMATIVE
    time.sleep(0.3)  # the deadline passes before the worker is scheduled

    assert store.execute_pending_async(db=None, cfg=Cfg()) is True
    assert results.event.wait(5)
    assert len(voice_tool.runs) == 1


def test_voice_approval_cannot_satisfy_a_dialog_tier(voice_tool):
    store = get_confirmation_store()
    _call("voiceTool")
    store.handle_voice_response("yes", language="en")
    req = store.get_pending().request
    assert store.is_authorised(req.tool_name, req.action, req.target, req.parameters,
                               required_tier=SafetyTier.CONFIRM_VOICE)
    assert not store.is_authorised(req.tool_name, req.action, req.target, req.parameters,
                                   required_tier=SafetyTier.CONFIRM_DIALOG)


def test_unclaimable_confirmation_reports_instead_of_executing(voice_tool, results):
    listener = _listener()
    _call("voiceTool")
    get_confirmation_store()._pending.expires_at = time.time() - 1

    assert listener._handle_pending_confirmation("yes", 1.0, 2.0) is False
    assert voice_tool.runs == []


# ---------------------------------------------------------------------------
# Desktop dialog lifetime (Qt)
# ---------------------------------------------------------------------------

def _request():
    return ConfirmationRequest("t", SafetyTier.CONFIRM_DIALOG, "uninstall", "App", {}, consequence="gone")


def _pump(qapp, until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        qapp.processEvents()
        time.sleep(0.01)
    return until()


def test_show_returns_immediately_and_confirm_resolves_once(qapp):
    from desktop_app.confirmation_dialog import show_desktop_confirmation_dialog

    resolved = []
    started = time.monotonic()
    handle = show_desktop_confirmation_dialog(_request(), resolved.append)
    assert time.monotonic() - started < 1.0
    assert resolved == []

    dlg = handle.dialog
    dlg.confirm_button.click()
    dlg.confirm_button.click()
    assert resolved == [True]


def test_cancel_and_close_both_resolve_false(qapp):
    from desktop_app.confirmation_dialog import show_desktop_confirmation_dialog

    cancelled, closed = [], []
    show_desktop_confirmation_dialog(_request(), cancelled.append).dialog.cancel_button.click()
    show_desktop_confirmation_dialog(_request(), closed.append).dialog.close()
    assert cancelled == [False]
    assert closed == [False]


def test_core_close_hides_the_dialog_and_ignores_late_clicks(qapp):
    from desktop_app.confirmation_dialog import show_desktop_confirmation_dialog

    resolved = []
    handle = show_desktop_confirmation_dialog(_request(), resolved.append)
    dlg = handle.dialog
    assert dlg.isVisible()

    handle.close()
    assert _pump(qapp, lambda: not dlg.isVisible())
    dlg.confirm_button.click()  # a click that raced the expiry

    assert resolved == []


def test_worker_thread_requests_are_shown_on_the_gui_thread(qapp):
    from PyQt6.QtCore import QThread
    from desktop_app.confirmation_dialog import show_desktop_confirmation_dialog

    box = {}
    resolved = []

    def worker():
        started = time.monotonic()
        box["handle"] = show_desktop_confirmation_dialog(_request(), resolved.append)
        box["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and box["elapsed"] < 1.0

    handle = box["handle"]
    assert _pump(qapp, lambda: handle.dialog is not None)
    assert handle.dialog.thread() == qapp.thread()
    handle.dialog.confirm_button.click()
    assert resolved == [True]


def test_close_before_the_dialog_opens_prevents_it(qapp):
    from desktop_app.confirmation_dialog import show_desktop_confirmation_dialog

    box = {}
    resolved = []
    thread = threading.Thread(
        target=lambda: box.setdefault("handle", show_desktop_confirmation_dialog(_request(), resolved.append)))
    thread.start()
    thread.join(2)
    box["handle"].close()  # expires before the GUI thread got to it
    _pump(qapp, lambda: False, timeout=0.3)

    assert box["handle"].dialog is None or not box["handle"].dialog.isVisible()
    assert resolved == []
