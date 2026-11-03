"""What the phone shows beside the chat: the pending confirmation, PC statistics, quick actions and voice.

The chat itself (messages, projects, models) is the web chat's hub. This adds the phone-only part of the poll
snapshot (``remote.spec.md``, Long poll) and answers confirmations. No text, token or audio is ever logged here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..debug import debug_log


class PhoneState:
    """The ``phone`` object of every poll. ``backend`` is a remote ``DaemonBackend`` or a fake; ``voice`` a ``PhoneVoice``."""

    def __init__(self, backend, voice, hub, *, quick_actions: List[str], allow_confirm: bool, secure: bool) -> None:
        self._backend = backend
        self._voice = voice
        self._hub = hub
        self._quick_actions = list(quick_actions)
        self._allow_confirm = allow_confirm
        self._secure = secure

    def confirmation(self) -> Optional[Dict[str, Any]]:
        """The pending desktop confirmation described for the phone, or ``None`` (always when the owner keeps
        confirmations on the desktop)."""
        return self._backend.pending_confirmation() if self._allow_confirm else None

    def snapshot(self, device_id: str) -> Dict[str, Any]:
        voice = dict(self._voice.availability(secure=self._secure))
        voice["audio"] = self._voice.audio_status(device_id)
        return {
            "confirmation": self.confirmation(),
            "pc": self._backend.pc_stats(),
            "quick_actions": list(self._quick_actions),
            "allow_confirm": self._allow_confirm,
            "voice": voice,
        }

    def signal(self) -> tuple:
        """A comparable value that changes when the confirmation or any reply audio status does."""
        confirmation = self.confirmation()
        return (confirmation["id"] if confirmation else None, self._voice.watch_value())

    def answer(self, request_id: str, approve: bool) -> bool:
        """Answer the pending confirmation. False when that request is no longer pending."""
        resolved = bool(self._backend.resolve_confirmation(request_id, approve))
        if resolved:
            debug_log(f"remote: confirmation {'approved' if approve else 'denied'} from the phone", "remote")
        self._hub.poke()
        return resolved

    @property
    def allow_confirm(self) -> bool:
        return self._allow_confirm
