"""Reply modes: which model answers requests, and switching between them at runtime.

``local`` is the offline baseline and starts nothing. A cloud mode (``codex``, ``claude``) runs its
bridge service, registered in ``runtime``, and can be switched to only when it is allowed in
Settings (``<mode>_enabled``), so a misheard command never sends anything to the cloud. The active
mode is process-wide runtime state initialised from ``reply_mode``; a switch is persisted back to it.
See ``bridge.spec.md``.
"""
from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from . import runtime

LOCAL, CODEX, CLAUDE = "local", "codex", "claude"
CLOUD_MODES = (CODEX, CLAUDE)
MODES = (LOCAL,) + CLOUD_MODES
LABELS = {LOCAL: "Local", CODEX: "ChatGPT (Codex)", CLAUDE: "Claude"}
_PROVIDERS = {CODEX: "OpenAI through Codex", CLAUDE: "Anthropic through Claude Code"}
_SERVICE_MODULES = {CODEX: "jarvis.codex_bridge", CLAUDE: "jarvis.claude_bridge"}


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    mode: str  # the active mode after the call
    previous: str
    reason: Optional[str] = None  # already | not_enabled | unknown_mode | start_failed


_lock = threading.RLock()
_switch_lock = threading.Lock()
_cfg: Any = None
_active = LOCAL
_service: Any = None
_factories: Dict[str, Callable[[Any], Any]] = {}
_save: Optional[Callable[[Dict[str, Any]], bool]] = None
_listeners: List[Callable[[str, List[str]], None]] = []
_warm: Optional[threading.Thread] = None


def enabled_modes(cfg: Any) -> List[str]:
    """Local plus the cloud modes allowed in Settings."""
    return [LOCAL] + [mode for mode in CLOUD_MODES if getattr(cfg, f"{mode}_enabled", False) is True]


def active_mode() -> str:
    with _lock:
        return _active


def state() -> Dict[str, Any]:
    with _lock:
        return {"mode": _active, "enabled": enabled_modes(_cfg)}


def update_cfg(cfg: Any) -> None:
    """Use ``cfg`` from now on (the local model changed); the active mode and its service are untouched."""
    global _cfg
    with _lock:
        if _cfg is not None:
            _cfg = cfg


def add_listener(callback: Callable[[str, List[str]], None]) -> None:
    """``callback(mode, enabled_modes)`` after the daemon starts and after every switch."""
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def remove_listener(callback: Callable[[str, List[str]], None]) -> None:
    with _lock:
        if callback in _listeners:
            _listeners.remove(callback)


def start(cfg: Any, *, factories: Optional[Dict[str, Callable[[Any], Any]]] = None,
          save: Optional[Callable[[Dict[str, Any]], bool]] = None) -> str:
    """Enter the configured mode when it is allowed, otherwise local. Returns the active mode."""
    global _cfg, _active, _factories, _save
    with _switch_lock:
        _stop_service("shutdown")
        with _lock:
            _cfg, _active = cfg, LOCAL
            _factories = dict(factories) if factories is not None else _default_factories()
            _save = save or _persist
        wanted = getattr(cfg, "reply_mode", LOCAL)
        if wanted in CLOUD_MODES:
            if wanted not in enabled_modes(cfg):
                print(f"🏠 {LABELS[wanted]} replies are not allowed in Settings, so replies stay local", flush=True)
                debug_log(f"configured reply mode {wanted} is not allowed; using local", "bridge")
            elif _start_service(wanted):
                with _lock:
                    _active = wanted
        mode = active_mode()
    debug_log(f"reply mode {mode} at start", "bridge")
    _notify()
    return mode


def switch(mode: str, *, persist: bool = True) -> SwitchResult:
    """Make ``mode`` the active reply mode. A request in flight is cancelled; a cloud mode that is not
    allowed is refused with nothing changed."""
    global _active
    with _switch_lock:
        previous = active_mode()
        if mode not in MODES:
            return SwitchResult(False, previous, previous, "unknown_mode")
        if mode not in enabled_modes(_cfg):
            debug_log(f"reply mode {mode} refused: not allowed", "bridge")
            return SwitchResult(False, previous, previous, "not_enabled")
        if mode == previous:
            return SwitchResult(True, mode, previous, "already")
        _stop_service("mode_switch")
        with _lock:
            _active = LOCAL
        if mode != LOCAL and not _start_service(mode):
            result = SwitchResult(False, LOCAL, previous, "start_failed")
        else:
            with _lock:
                _active = mode
            if persist and _save is not None:
                _save({"reply_mode": mode})
            result = SwitchResult(True, mode, previous)
        print(f"🔀 Reply mode: {LABELS[result.mode]}", flush=True)
        debug_log(f"reply mode {previous} -> {result.mode} ({result.reason or 'ok'})", "bridge")
        if result.ok and previous == LOCAL and result.mode in CLOUD_MODES:
            threading.Thread(target=_release_local_models, args=(_cfg, result.mode), daemon=True,
                             name="reply-model-release").start()
    _notify()
    return result


def _release_local_models(cfg: Any, mode: str) -> None:
    """Unload the models only local replies use, so a cloud session does not keep them resident."""
    from ..llm import get_llm_backend
    from ..llm.tiers import local_reply_models
    try:
        models = local_reply_models(cfg)
        backend = get_llm_backend(cfg) if models else None
        for model in models:
            if backend.release(model):
                print(f"  🧹 Unloaded local model '{model}' ({LABELS[mode]} replies do not use it)", flush=True)
                debug_log(f"released local reply model {model}", "bridge")
    except Exception as exc:
        debug_log(f"local model release failed: {type(exc).__name__}", "bridge")


def stop() -> None:
    """Cancel any request and stop the active bridge's processes. Safe to repeat."""
    with _switch_lock:
        _stop_service("shutdown")


def wait_for_warm_up(timeout_sec: float) -> bool:
    """Wait for the active bridge's start-up check. Returns whether it finished in time."""
    with _lock:
        warm = _warm
    if warm is None:
        return True
    warm.join(timeout_sec)
    return not warm.is_alive()


def reset() -> None:
    """Let a running start-up check finish, stop everything and forget the configuration and listeners."""
    global _cfg, _active, _factories, _save
    wait_for_warm_up(10)
    stop()
    with _lock:
        _cfg, _active, _factories, _save = None, LOCAL, {}, None
        _listeners.clear()


# -- internals ---------------------------------------------------------------------------


def _default_factories() -> Dict[str, Callable[[Any], Any]]:
    def build(mode: str) -> Callable[[Any], Any]:
        def factory(cfg: Any) -> Any:
            return importlib.import_module(f"{_SERVICE_MODULES[mode]}.lifecycle").create_service(cfg)
        return factory
    return {mode: build(mode) for mode in CLOUD_MODES}


def _persist(values: Dict[str, Any]) -> bool:
    from ..config import update_config_values
    return update_config_values(values)


def _failure_text(mode: str, reason: str) -> str:
    return importlib.import_module(f"{_SERVICE_MODULES[mode]}.service").failure_text(reason)


def _start_service(mode: str) -> bool:
    global _service, _warm
    try:
        service = _factories[mode](_cfg)
    except Exception as exc:
        debug_log(f"{mode} bridge setup failed: {type(exc).__name__}", "bridge")
        print(f"  ❌ {LABELS[mode]} replies could not start ({type(exc).__name__}). Replies stay local.",
              flush=True)
        return False
    warm = threading.Thread(target=_warm_up, args=(mode, service), daemon=True, name=f"{mode}-warm-up")
    with _lock:
        _service, _warm = service, warm
    runtime.set_service(service)
    print(f"🛰️ {LABELS[mode]} replies in the background", flush=True)
    print(f"  ☁️ Requests, permitted context and tool results are sent to {_PROVIDERS[mode]}", flush=True)
    print("  🏠 Say \"go local\" or choose Local in the tray to switch back at any time", flush=True)
    warm.start()
    return True


def _warm_up(mode: str, service: Any) -> None:
    """Check the bridge before the first request, reporting problems early."""
    failure = service.prepare()
    if runtime.get_service() is not service:
        return
    if failure is None:
        print(f"  ✅ {LABELS[mode]} is ready in the background", flush=True)
    else:
        print(f"  ⚠️ {_failure_text(mode, failure)}", flush=True)


def _stop_service(reason: str) -> None:
    global _service, _warm
    with _lock:
        service, _service, _warm = _service, None, None
    if service is None:
        return
    runtime.set_service(None)
    try:
        service.cancel_active(reason)
    except Exception as exc:
        debug_log(f"bridge cancel failed: {type(exc).__name__}", "bridge")
    try:
        service.close()
    except Exception as exc:
        debug_log(f"bridge service close failed: {type(exc).__name__}", "bridge")


def _notify() -> None:
    with _lock:
        mode, enabled, listeners = _active, enabled_modes(_cfg), list(_listeners)
    for callback in listeners:
        try:
            callback(mode, list(enabled))
        except Exception as exc:
            debug_log(f"reply mode listener failed: {type(exc).__name__}", "bridge")
