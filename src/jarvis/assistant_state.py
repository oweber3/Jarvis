"""Jarvis's overall state (asleep, idle, listening, thinking, speaking, dictating) and who hears about it.

Every state change goes through ``set_state``. It forwards the state to the desktop face (when the desktop
app is importable) and notifies in-process subscribers such as local extensions, which must not
depend on the desktop app. Subscribers run on the caller's thread, so they must return quickly.
"""
from __future__ import annotations

import threading
from enum import Enum
from typing import Callable, List

from .debug import debug_log


class AssistantState(Enum):
    ASLEEP = "asleep"                              # daemon not started yet
    IDLE = "idle"                                  # awake, waiting for the wake word
    LISTENING = "listening"                        # collecting a request or in the hot window
    THINKING = "thinking"                          # working on a reply
    SPEAKING = "speaking"                          # speaking a reply
    DICTATING = "dictating"                        # hold-to-dictate recording
    DICTATION_PROCESSING = "dictation_processing"  # transcribing and pasting dictation


Subscriber = Callable[[AssistantState], None]

_lock = threading.Lock()
_state = AssistantState.ASLEEP
_subscribers: List[Subscriber] = []


def current() -> AssistantState:
    with _lock:
        return _state


def subscribe(callback: Subscriber) -> Callable[[], None]:
    """Call ``callback(state)`` on every change. Returns a function that unsubscribes."""
    with _lock:
        _subscribers.append(callback)

    def unsubscribe() -> None:
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return unsubscribe


def set_state(state: AssistantState) -> None:
    """Record ``state``, show it on the desktop face and tell subscribers if it changed."""
    global _state
    _show_on_face(state)
    with _lock:
        if state is _state:
            return
        _state = state
        subscribers = list(_subscribers)
    debug_log(f"assistant state: {state.value}", "state")
    for callback in subscribers:
        try:
            callback(state)
        except Exception as exc:  # a subscriber must never break the caller or the others
            debug_log(f"assistant state subscriber failed: {exc}", "state")


def _show_on_face(state: AssistantState) -> None:
    try:
        from desktop_app.face_widget import JarvisState, get_jarvis_state
        get_jarvis_state().set_state(JarvisState(state.value))
    except Exception as exc:  # headless runs have no desktop app
        debug_log(f"face state not shown ({state.value}): {exc}", "state")


def reset() -> None:
    """Back to ASLEEP with no subscribers (tests and daemon shutdown)."""
    global _state
    with _lock:
        _state = AssistantState.ASLEEP
        _subscribers.clear()
