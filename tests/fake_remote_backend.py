"""A stand-in for the daemon behind phone access: confirmations, PC statistics, speech in and out."""
from __future__ import annotations

from typing import List, Optional, Tuple


class FakeRemoteBackend:
    """Mirrors ``jarvis.remote.backend.DaemonBackend``. The chat itself is the web chat's fake backend."""

    def __init__(self) -> None:
        self.confirmation: Optional[dict] = None
        self.resolved: List[tuple] = []
        self.stats: Optional[dict] = {"cpu": 12.0, "ram": 40.0}
        self.busy = False
        # Speech in: why transcription cannot work now, what it returns, and the samples it was given.
        self.voice_blocker: Optional[str] = None
        self.transcript = "what time is it"
        self.transcribed: list = []
        # Speech out: raw 16-bit mono samples and their rate (None: no voice could speak), and the texts.
        self.speech: Optional[Tuple[bytes, int]] = (b"\x01\x00\x02\x00" * 400, 22050)
        self.render_error: Optional[Exception] = None
        self.rendered: List[str] = []

    def pending_confirmation(self) -> Optional[dict]:
        return self.confirmation

    def resolve_confirmation(self, request_id: str, approve: bool) -> bool:
        if self.confirmation is None or self.confirmation["id"] != request_id:
            return False
        self.resolved.append((request_id, approve))
        self.confirmation = None
        return True

    def pc_stats(self) -> Optional[dict]:
        return self.stats

    def is_busy(self) -> bool:
        return self.busy

    def voice_reason(self) -> Optional[str]:
        return self.voice_blocker

    def transcribe(self, samples) -> str:
        self.transcribed.append(samples)
        return self.transcript

    def render_speech(self, text: str) -> Optional[Tuple[bytes, int]]:
        self.rendered.append(text)
        if self.render_error is not None:
            raise self.render_error
        return self.speech
