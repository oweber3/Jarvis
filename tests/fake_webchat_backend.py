"""A stand-in for the daemon behind the web chat: a real dialogue memory and a scripted reply engine."""
from __future__ import annotations

import threading
from typing import Callable, List, Optional, Tuple

from jarvis.memory.conversation import DialogueMemory


class FakeWebChatBackend:
    """Mirrors ``jarvis.webchat.backend.DaemonBackend``. ``mode`` decides how each query ends:
    ``reply`` (store the turn and answer), ``busy``, ``unavailable`` (answer ``None`` at once),
    ``fail`` (start, then ``None``) or ``hold`` (start and wait until ``release()``)."""

    def __init__(self) -> None:
        self.memory: Optional[DialogueMemory] = DialogueMemory()
        self.state = "idle"
        self.mode = "reply"
        self.reply_text = "Done."
        self.busy = False
        self.cancelled = 0
        self.submitted: List[str] = []
        self.reply_mode = {"mode": "local", "enabled": ["local", "claude"]}
        self.local_model = {"current": "gemma4:12b", "switchable": True}
        self.models = [{"id": "gemma4:12b", "name": "Gemma 4 12B", "installed": True},
                       {"id": "qwen3.5:9b", "name": "Qwen 3.5 9B", "installed": True}]
        # The active cloud mode's choice (None in local mode) and the models its bridge reported.
        self.cloud: Optional[dict] = None
        self.cloud_options: List[dict] = []
        self.refuse_cloud: Optional[str] = None
        self.cloud_calls: List[tuple] = []
        self.refuse_mode: Optional[str] = None
        self.refuse_model: Optional[str] = None
        self.switch_calls: List[list] = []
        self._subscribers: List[Callable[[str], None]] = []
        self._held: Optional[tuple] = None

    # -- backend interface ---------------------------------------------------

    def is_ready(self) -> bool:
        return self.memory is not None

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
            _text, on_complete = self._held
            self._held = None
            on_complete(None)

    def release(self) -> None:
        if self._held is not None:
            text, on_complete = self._held
            self._held = None
            self._answer(text, on_complete)

    def is_busy(self) -> bool:
        return self.busy

    def dialogue_memory(self):
        return self.memory

    def assistant_state(self) -> str:
        return self.state

    def subscribe_state(self, callback: Callable[[str], None]) -> Callable[[], None]:
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    def reply_mode_state(self) -> Optional[dict]:
        return self.reply_mode

    def switch_reply_mode(self, mode: str) -> Tuple[bool, Optional[str]]:
        if self.refuse_mode:
            return False, self.refuse_mode
        self.reply_mode = {**self.reply_mode, "mode": mode}
        return True, None

    def local_model_state(self) -> dict:
        return self.local_model

    def local_models(self) -> list:
        return self.models

    def cloud_model_state(self) -> Optional[dict]:
        return self.cloud

    def cloud_models(self) -> List[dict]:
        return self.cloud_options

    def set_cloud_model(self, model: str, effort: Optional[str]) -> Tuple[bool, Optional[str]]:
        self.cloud_calls.append((model, effort))
        if self.refuse_cloud:
            return False, self.refuse_cloud
        self.cloud = {**(self.cloud or {}), "model": model, "effort": effort or ""}
        return True, None

    def set_local_model(self, model: str) -> Tuple[bool, Optional[str]]:
        if self.refuse_model:
            return False, self.refuse_model
        self.local_model = {**self.local_model, "current": model}
        return True, None

    def switch_conversation(self, messages: list) -> bool:
        self.switch_calls.append(messages)
        if self.busy or self.memory is None:
            return False
        self.memory.set_messages(messages, saved=True)
        return True

    # -- helpers ---------------------------------------------------------------

    def speak(self, user: str, reply: str) -> None:
        """A voice exchange: the same two turns a spoken request adds to the shared memory."""
        self.memory.add_message("user", user)
        self.memory.add_message("assistant", reply)

    def set_state(self, state: str) -> None:
        self.state = state
        for callback in list(self._subscribers):
            callback(state)

    def _answer(self, text: str, on_complete) -> None:
        self.memory.add_message("user", text)
        self.memory.add_message("assistant", self.reply_text)
        on_complete(self.reply_text)
