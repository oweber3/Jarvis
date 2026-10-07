"""The only bridge between the web chat and the rest of Jarvis: the daemon, reply modes and local models."""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

from .. import assistant_state
from ..debug import debug_log

INSTALLED_CACHE_SEC = 30.0
# The settings keys of each cloud reply mode: (model, effort).
CLOUD_KEYS = {"codex": ("codex_model", "codex_reasoning_effort"), "claude": ("claude_model", "claude_effort")}


class DaemonBackend:
    """What the web chat needs from the running daemon. Every call returns quickly except a mode switch."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._installed: Optional[set] = None
        self._installed_at = 0.0

    # -- requests ----------------------------------------------------------------

    def is_ready(self) -> bool:
        from .. import daemon
        return daemon.get_dialogue_memory() is not None and daemon.get_settings() is not None

    def submit(self, text: str, *, on_start, on_complete, on_busy) -> None:
        from .. import daemon
        daemon.submit_text_query(text, on_start=on_start, on_complete=on_complete, on_busy=on_busy, origin="chat")

    def cancel(self) -> None:
        from .. import daemon
        daemon.cancel_active_chat_query()

    def is_busy(self) -> bool:
        from .. import daemon
        return daemon.is_query_running()

    def dialogue_memory(self):
        from .. import daemon
        return daemon.get_dialogue_memory()

    def switch_conversation(self, messages: list) -> bool:
        from .. import daemon
        return bool(daemon.switch_chat_conversation(messages))

    # -- state -------------------------------------------------------------------

    def assistant_state(self) -> str:
        return assistant_state.current().value

    def subscribe_state(self, callback: Callable[[str], None]) -> Callable[[], None]:
        return assistant_state.subscribe(lambda state: callback(state.value))

    # -- reply modes -------------------------------------------------------------

    def reply_mode_state(self) -> Optional[dict]:
        try:
            from ..bridge import modes
            return modes.state()
        except Exception as exc:
            debug_log(f"webchat: reply mode unavailable: {type(exc).__name__}", "webchat")
            return None

    def switch_reply_mode(self, mode: str) -> Tuple[bool, Optional[str]]:
        from .. import daemon
        result = daemon.set_reply_mode(mode)
        return (True, None) if result.ok else (False, result.reason)

    # -- cloud models ------------------------------------------------------------

    def cloud_model_state(self) -> Optional[dict]:
        """The active Claude or Codex mode's model and effort, or ``None`` in local mode. ``ready`` says
        whether the bridge has reported the models it offers."""
        from .. import daemon
        from ..bridge import modes, runtime
        mode = modes.active_mode()
        cfg = daemon.get_settings()
        if mode not in CLOUD_KEYS or cfg is None:
            return None
        model_key, effort_key = CLOUD_KEYS[mode]
        service = runtime.get_service()
        return {"mode": mode, "model": str(getattr(cfg, model_key, "") or ""),
                "effort": str(getattr(cfg, effort_key, "") or ""),
                "ready": service is not None and bool(service.available_models())}

    def cloud_models(self) -> List[dict]:
        """The models (with their efforts) the active cloud bridge reported; empty in local mode."""
        from ..bridge import modes, runtime
        service = runtime.get_service()
        if modes.active_mode() not in CLOUD_KEYS or service is None:
            return []
        return [model.to_dict() for model in service.available_models()]

    def set_cloud_model(self, model: str, effort: Optional[str]) -> Tuple[bool, Optional[str]]:
        from .. import daemon
        result = daemon.set_cloud_model(model, effort)
        return (True, None) if result.ok else (False, result.reason)

    # -- local models ------------------------------------------------------------

    def local_model_state(self) -> dict:
        from .. import daemon
        cfg = daemon.get_settings()
        if cfg is None:
            return {"current": None, "switchable": False}
        return {"current": str(cfg.llm_chat_model), "switchable": cfg.llm_provider == "ollama"}

    def local_models(self) -> List[dict]:
        """The offered local models (and the current one) with whether the runtime has each installed."""
        from .. import daemon
        from ..config import OFFERED_CHAT_MODELS
        cfg = daemon.get_settings()
        if cfg is None:
            return []
        current = str(cfg.llm_chat_model)
        if cfg.llm_provider != "ollama":
            return [{"id": current, "name": current, "installed": True}]
        installed = self._installed_models(cfg)
        models = [{"id": model_id, "name": info["name"], "installed": model_id in installed}
                  for model_id, info in OFFERED_CHAT_MODELS.items()]
        if current not in OFFERED_CHAT_MODELS:
            models.append({"id": current, "name": current, "installed": True})
        return models

    def _installed_models(self, cfg) -> set:
        now = self._clock()
        if self._installed is not None and now - self._installed_at < INSTALLED_CACHE_SEC:
            return self._installed
        try:
            from .. import llm
            self._installed = set(llm.get_llm_backend(cfg).list_models())
        except Exception as exc:
            debug_log(f"webchat: could not list local models: {type(exc).__name__}", "webchat")
            self._installed = set()
        self._installed_at = now
        return self._installed

    def set_local_model(self, model: str) -> Tuple[bool, Optional[str]]:
        from .. import daemon
        result = daemon.set_local_chat_model(model)
        self._installed = None  # a model loaded or released changes what the runtime reports
        return (True, None) if result.ok else (False, result.reason)
