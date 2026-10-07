"""A confirmed action started from text chat reports back into the chat.

The dialog answer arrives asynchronously, so the outcome is delivered through
an origin-aware result handler. Chat-origin outcomes reach the chat window via
a Qt signal (GUI thread) and are recorded in the shared dialogue memory; voice
outcomes keep the spoken path.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import jarvis.tools.confirmation as confirmation
from jarvis import daemon
from jarvis.memory.conversation import DialogueMemory
from jarvis.tools.base import Tool
from jarvis.tools.confirmation import (
    ConfirmationRequest,
    SafetyTier,
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


class UninstallTool(Tool):
    def __init__(self):
        self.runs: list[int] = []

    @property
    def name(self):
        return "uninstallTool"

    @property
    def description(self):
        return "test"

    @property
    def inputSchema(self):
        return {"type": "object", "properties": {"target": {"type": "string"}}}

    def classify_safety(self, args, cfg):
        return ConfirmationRequest(self.name, SafetyTier.CONFIRM_DIALOG, "uninstall",
                                   str((args or {}).get("target", "App")), dict(args or {}))

    def run(self, args, context):
        self.runs.append(threading.get_ident())
        return ToolExecutionResult(success=True, reply_text="Uninstalled App.")


class FakeDialogs:
    def __init__(self):
        self.resolves = []
        self.handles = []

    def __call__(self, request, resolve):
        handle = SimpleNamespace(closed=threading.Event(), close=lambda: None)
        handle.close = handle.closed.set
        self.resolves.append(resolve)
        self.handles.append(handle)
        return handle


@pytest.fixture
def tool():
    t = UninstallTool()
    BUILTIN_TOOLS["uninstallTool"] = t
    yield t
    BUILTIN_TOOLS.pop("uninstallTool", None)


@pytest.fixture
def memory(monkeypatch):
    dm = DialogueMemory()
    monkeypatch.setattr(daemon, "_global_dialogue_memory", dm)
    return dm


@pytest.fixture
def dialogs():
    fake = FakeDialogs()
    set_dialog_callback(fake)
    yield fake
    set_dialog_callback(None)


@pytest.fixture(autouse=True)
def clean_state():
    store = get_confirmation_store()
    store.clear_pending()
    for origin in (confirmation.ORIGIN_VOICE, confirmation.ORIGIN_CHAT):
        set_result_handler(None, origin=origin)
    daemon._chat_result_listeners.clear()
    yield
    store.clear_pending()
    for origin in (confirmation.ORIGIN_VOICE, confirmation.ORIGIN_CHAT):
        set_result_handler(None, origin=origin)
    daemon._chat_result_listeners.clear()


@pytest.fixture
def chat(qapp, monkeypatch, memory):
    """A bundled-mode chat window wired to the daemon's chat result delivery."""
    monkeypatch.setattr("desktop_app.chat_window.get_hot_window_messages", lambda: [])
    from desktop_app.chat_window import ChatWindow

    win = ChatWindow()
    daemon.install_chat_result_delivery()
    yield win
    win.close()


def _chat_request():
    # Text chat runs the engine with quiet=True.
    return run_tool_with_retries(
        db=None, cfg=Cfg(), tool_name="uninstallTool", tool_args={"target": "App"},
        system_prompt="", original_prompt="", redacted_text="", quiet=True,
    )


def _pump(qapp, until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        qapp.processEvents()
        time.sleep(0.01)
    return until()


def _assistant_texts(win):
    return [m["text"] for m in win._messages if m["kind"] == "assistant"]


def test_chat_dialog_confirm_posts_the_result_in_chat(qapp, chat, dialogs, tool, memory):
    assert _chat_request().success is False

    dialogs.resolves[0](True)

    assert _pump(qapp, lambda: "Uninstalled App." in _assistant_texts(chat))
    # The shared conversation remembers the outcome too.
    assert {"role": "assistant", "content": "Uninstalled App."}.items() <= memory.all_messages()[-1].items()


def test_chat_dialog_cancel_posts_nothing(qapp, chat, dialogs, tool):
    _chat_request()
    dialogs.resolves[0](False)

    _pump(qapp, lambda: False, timeout=0.3)
    assert tool.runs == []
    assert _assistant_texts(chat) == []


def test_expired_request_posts_nothing(qapp, chat, dialogs, tool, monkeypatch):
    monkeypatch.setattr(confirmation, "DIALOG_TIMEOUT_SEC", 0.05)
    _chat_request()
    assert dialogs.handles[0].closed.wait(2)

    dialogs.resolves[0](True)  # late click
    _pump(qapp, lambda: False, timeout=0.3)
    assert tool.runs == []
    assert _assistant_texts(chat) == []


def test_result_is_posted_exactly_once(qapp, chat, dialogs, tool):
    _chat_request()
    dialogs.resolves[0](True)
    dialogs.resolves[0](True)

    assert _pump(qapp, lambda: _assistant_texts(chat))
    _pump(qapp, lambda: False, timeout=0.3)
    assert _assistant_texts(chat) == ["Uninstalled App."]
    assert len(tool.runs) == 1


def test_voice_origin_still_uses_the_voice_path(qapp, chat, dialogs, tool):
    spoken, done = [], threading.Event()
    set_result_handler(lambda reply, ok: (spoken.append((reply, ok)), done.set()),
                       origin=confirmation.ORIGIN_VOICE)
    run_tool_with_retries(
        db=None, cfg=Cfg(), tool_name="uninstallTool", tool_args={"target": "App"},
        system_prompt="", original_prompt="", redacted_text="", quiet=False,
    )

    dialogs.resolves[0](True)

    assert done.wait(5)
    assert spoken == [("Uninstalled App.", True)]
    _pump(qapp, lambda: False, timeout=0.3)
    assert _assistant_texts(chat) == []


def test_chat_ui_is_only_touched_on_the_gui_thread(qapp, chat, dialogs, tool, monkeypatch):
    touched = []
    original = chat._append_assistant
    monkeypatch.setattr(chat, "_append_assistant",
                        lambda text: (touched.append(threading.get_ident()), original(text)))
    _chat_request()
    dialogs.resolves[0](True)

    assert _pump(qapp, lambda: touched)
    assert touched == [threading.get_ident()]
    assert tool.runs[0] != threading.get_ident()


def test_destroyed_chat_window_is_handled_safely(qapp, chat, dialogs, tool, memory):
    from PyQt6 import sip

    _chat_request()
    sip.delete(chat.signals)  # the window's signal bridge is gone

    dialogs.resolves[0](True)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not tool.runs:
        time.sleep(0.01)
    time.sleep(0.2)
    qapp.processEvents()
    assert len(tool.runs) == 1  # the action still ran; delivery failed quietly
    assert memory.all_messages()[-1]["content"] == "Uninstalled App."


def test_no_chat_window_still_records_the_result(dialogs, tool, memory):
    daemon.install_chat_result_delivery()
    _chat_request()
    dialogs.resolves[0](True)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not memory.all_messages():
        time.sleep(0.01)
    assert memory.all_messages()[-1]["content"] == "Uninstalled App."


def test_closing_the_chat_window_before_completion_keeps_the_result(qapp, chat, dialogs, tool):
    _chat_request()
    chat.close()  # hides; the instance stays alive for the tray to re-show

    dialogs.resolves[0](True)

    assert _pump(qapp, lambda: "Uninstalled App." in _assistant_texts(chat))
