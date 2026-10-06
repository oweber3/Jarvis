"""A stand-in for the daemon behind phone access: a real dialogue memory and a scripted reply engine."""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

from jarvis.memory.conversation import DialogueMemory


class FakeBackend:
    """Mirrors ``DaemonBackend``. ``reply_with`` decides how each submitted query ends.

    ``mode`` is ``"reply"`` (store the turn and answer), ``"busy"``, ``"unavailable"`` (answer ``None``
    at once, like a daemon that has not booted), ``"fail"`` (start, then ``None``) or ``"hold"`` (start
    and wait until ``release()``).
    """

    def __init__(self) -> None:
        self.memory: Optional[DialogueMemory] = DialogueMemory()
        self.state = "idle"
        self.mode = "reply"
        self.reply_text = "Done."
        self.busy = False
        self.cancelled = 0
        self.submitted: List[str] = []
        self.confirmation: Optional[dict] = None
        self.resolved: List[tuple] = []
        self.reply_mode = {"mode": "local", "enabled": ["local"]}
        self.stats = {"cpu": 12.0, "ram": 40.0}
        self._subscribers: List[Callable[[str], None]] = []
        self._release = threading.Event()
        self._held: Optional[tuple] = None

    # -- backend interface ---------------------------------------------------

    def submit(self, text: str, *, on_start, on_complete, on_busy) -> None:
        self.submitted.append(text)
        if self.mode == "busy":
            on_busy()
            return
        if self.mode == "unavailable":
            on_complete(None)
            return
        on_start(text)
        if self.mode == "fail":
            on_complete(None)
            return
        if self.mode == "hold":
            self._held = (text, on_complete)
            return
        self._answer(text, on_complete)

    def cancel(self) -> None:
        self.cancelled += 1
        if self._held is not None:
            text, on_complete = self._held
            self._held = None
            on_complete(None)

    def release(self) -> None:
        if self._held is not None:
            text, on_complete = self._held
            self._held = None
            self._answer(text, on_complete)

    def assistant_state(self) -> str:
        return self.state

    def subscribe_state(self, callback: Callable[[str], None]) -> Callable[[], None]:
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    def dialogue_memory(self):
        return self.memory

    def is_busy(self) -> bool:
        return self.busy

    def pending_confirmation(self) -> Optional[dict]:
        return self.confirmation

    def resolve_confirmation(self, request_id: str, approve: bool) -> bool:
        if self.confirmation is None or self.confirmation["id"] != request_id:
            return False
        self.resolved.append((request_id, approve))
        self.confirmation = None
        return True

    def reply_mode_state(self) -> Optional[dict]:
        return self.reply_mode

    def pc_stats(self) -> Optional[dict]:
        return self.stats

    # -- helpers ---------------------------------------------------------------

    def set_state(self, state: str) -> None:
        self.state = state
        for callback in list(self._subscribers):
            callback(state)

    def _answer(self, text: str, on_complete) -> None:
        self.memory.add_message("user", text)
        self.memory.add_message("assistant", self.reply_text)
        on_complete(self.reply_text)
