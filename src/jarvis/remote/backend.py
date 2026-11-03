"""The only bridge between phone access and the rest of Jarvis: confirmations, statistics, speech in and out.

The chat itself (requests, the conversation, models) is the web chat's backend (``webchat/backend.py``)."""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

from .. import assistant_state
from ..debug import debug_log


_BUSY_STATES = frozenset({
    assistant_state.AssistantState.LISTENING, assistant_state.AssistantState.THINKING,
    assistant_state.AssistantState.SPEAKING, assistant_state.AssistantState.DICTATING,
    assistant_state.AssistantState.DICTATION_PROCESSING,
})


class DaemonBackend:
    """What phone access needs from the running daemon. Every call returns quickly except speech."""

    def __init__(self, confirmation_store=None) -> None:
        self._store = confirmation_store
        self._fallback_voice = None

    def _confirmations(self):
        if self._store is not None:
            return self._store
        from ..tools.confirmation import get_confirmation_store
        return get_confirmation_store()

    # -- state -------------------------------------------------------------------

    def is_busy(self) -> bool:
        """A request is running, or Jarvis is listening or speaking at the PC."""
        from .. import daemon
        return daemon.is_query_running() or assistant_state.current() in _BUSY_STATES

    def pc_stats(self) -> Optional[dict]:
        try:
            import psutil
            return {"cpu": float(psutil.cpu_percent(interval=None)),
                    "ram": float(psutil.virtual_memory().percent)}
        except Exception:
            return None

    # -- confirmations -----------------------------------------------------------

    def pending_confirmation(self) -> Optional[dict]:
        """The pending desktop-dialog confirmation, described for the phone, or ``None``."""
        from ..tools.confirmation import SafetyTier
        pending = self._confirmations().get_pending()
        if pending is None or pending.request.tier != SafetyTier.CONFIRM_DIALOG:
            return None
        request = pending.request
        return {
            "id": request.id,
            "tool": request.tool_name,
            "action": request.action,
            "target": request.target,
            "consequence": request.consequence or "",
            "expires_in": max(0, int(pending.expires_at - time.time())),
        }

    def resolve_confirmation(self, request_id: str, approve: bool) -> bool:
        """Answer the pending dialog request. True when the answer was applied (approved and
        scheduled, or denied); False when that request is no longer pending."""
        store = self._confirmations()
        current = self.pending_confirmation()
        if current is None or current["id"] != request_id:
            return False
        applied = store.resolve_dialog(request_id, bool(approve))
        return applied if approve else True

    # -- speech in ---------------------------------------------------------------

    def voice_reason(self) -> Optional[str]:
        """Why a spoken request cannot be transcribed now (``disabled``, ``unavailable``, ``not_ready``), or None.

        Transcription uses the dictation engine's Whisper model, so dictation must be on in Settings."""
        from .. import daemon
        cfg = daemon.get_settings()
        if cfg is not None and getattr(cfg, "dictation_enabled", True) is False:
            return "disabled"
        engine = daemon.get_dictation_engine()
        if engine is None:
            return "unavailable"
        return engine.transcribe_blocker()

    def transcribe(self, samples) -> str:
        """The words in ``samples`` (16 kHz mono float32). Never logs them."""
        from .. import daemon
        engine = daemon.get_dictation_engine()
        return engine.transcribe_samples(samples, 16000) if engine is not None else ""

    # -- speech out --------------------------------------------------------------

    def render_speech(self, text: str) -> Optional[Tuple[bytes, int]]:
        """``text`` spoken in Jarvis's own voice as 16-bit mono samples and their rate, or ``None`` when no voice
        could speak it. Nothing is played at the PC."""
        engine = self._voice_engine()
        if engine is None:
            return None
        samples, rate = engine.render(text)
        if samples is None or not len(samples):
            return None
        return samples.astype("<i2").tobytes(), int(rate)

    def _voice_engine(self):
        """The live engine when it can render (Piper, Kokoro); for Chatterbox, a Piper voice that is already on
        disk. A render never downloads a voice."""
        from .. import daemon
        live = daemon.get_tts_engine()
        if live is not None and hasattr(live, "render"):
            return live
        cfg = daemon.get_settings()
        if cfg is None:
            return None
        if self._fallback_voice is None:
            from ..output.piper_voices import piper_voice_downloaded
            from ..output.tts import create_tts_engine
            custom = str(getattr(cfg, "tts_piper_model_path", "") or "")
            present = (os.path.exists(os.path.expanduser(custom)) if custom
                       else piper_voice_downloaded(cfg.tts_piper_voice))
            if not present:
                debug_log("remote: no Piper voice on disk to stand in for the active voice", "remote")
                return None
            self._fallback_voice = create_tts_engine(
                engine="piper", enabled=True, piper_model_path=custom or None, piper_voice=cfg.tts_piper_voice,
                piper_speaker=cfg.tts_piper_speaker, piper_length_scale=cfg.tts_piper_length_scale,
                piper_noise_scale=cfg.tts_piper_noise_scale, piper_noise_w=cfg.tts_piper_noise_w)
        return self._fallback_voice
