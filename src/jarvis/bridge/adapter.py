"""Background reply path used by the reply engine in a cloud reply mode (``codex`` or ``claude``).

Runs after redaction, pending-confirmation handling and the fast-command path, and replaces the
local planner, router, enrichment and chat loop. Failures are explicit and never fall back to the
local model. See ``bridge.spec.md``.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from ..utils.redact import redact
from . import runtime
from .settings import BridgeSettings, bridge_settings

_MESSAGE_CHARS = 1000
MODE_LABELS = {"codex": "Codex", "claude": "Claude"}


def build_dialogue_context(dialogue_memory: Any, settings: BridgeSettings) -> List[Dict[str, str]]:
    """Bounded, redacted recent user and assistant messages shared with the bridge.

    Turns kept out of the diary because they quote the activity log stay local unless the user
    shares the activity log with cloud modes (``memory/activity_log.spec.md``)."""
    if not settings.share_recent_dialogue or dialogue_memory is None:
        return []
    limit = settings.recent_dialogue_messages
    try:
        messages = (dialogue_memory.get_recent_messages(include_private=settings.share_activity_log)
                    if dialogue_memory.has_recent_messages() else [])
    except Exception as exc:
        debug_log(f"dialogue context unavailable: {type(exc).__name__}", "bridge")
        return []
    shared = [
        {"role": m["role"], "content": redact(str(m.get("content") or ""))[:_MESSAGE_CHARS]}
        for m in messages
        if m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip()
    ]
    return shared[-limit:] if limit else []


def build_desktop_context(cfg: Any, settings: BridgeSettings, origin: str,
                          read_foreground: Callable[[], Any] = lambda: None) -> Dict[str, Any]:
    """The desktop part of a request: the window the user is looking at and Jarvis's own records of
    what it acted on in this conversation, each under its sharing switch, with one reference-data note.

    The foreground is read only when it is shared and the request was made at the PC. No titles, paths
    or content: see ``memory/desktop_referents.spec.md``."""
    from ..memory import desktop_referents as dr
    desktop: Dict[str, Any] = {}
    if settings.share_foreground_window and origin != "phone":
        foreground = dr.foreground_for_request(read_foreground())
        if foreground is not None:
            desktop["foreground_window"] = foreground
    if settings.share_desktop_referents:
        windows = dr.records_for_request(dr.live_referents(cfg))
        others = dr.others_for_request(dr.live_others(cfg))
        if windows:
            desktop["desktop_referents"] = windows
        if others:
            desktop["other_referents"] = others
    if desktop:
        desktop["desktop_referents_note"] = dr.request_note()
    debug_log(f"sharing desktop context: foreground={'foreground_window' in desktop}, "
              f"{len(desktop.get('desktop_referents', []))} window(s), "
              f"{len(desktop.get('other_referents', []))} other(s)", "bridge")
    return desktop


def not_running_text(mode: str) -> str:
    label = MODE_LABELS.get(mode, mode)
    return (f"Background {label} mode is not running. Say \"go local\" or choose Local in the tray "
            "to switch Jarvis to local mode.")


def run_bridge_reply(mode: str, db: Any, cfg: Any, tts: Any, redacted: str, dialogue_memory: Any,
                     language: Optional[str], quiet: bool, deliver: Callable[..., str],
                     origin: Optional[str] = None,
                     read_foreground: Callable[[], Any] = lambda: None) -> Optional[str]:
    """Hand ``redacted`` to the running bridge of ``mode`` and deliver the validated result once."""
    service = runtime.get_service()
    if service is None or getattr(service, "mode", mode) != mode:
        debug_log(f"{mode} mode without a running bridge", "bridge")
        return deliver(not_running_text(mode), redacted, cfg, tts, dialogue_memory, quiet)
    settings = bridge_settings(cfg, mode)
    origin = origin or ("chat" if quiet else "voice")
    outcome = service.run_request(
        redacted, build_dialogue_context(dialogue_memory, settings),
        origin, language, db, quiet,
        desktop=build_desktop_context(cfg, settings, origin, read_foreground),
    )
    debug_log(f"{mode} request outcome: {outcome.kind}" + (f" ({outcome.reason})" if outcome.reason else ""),
              "bridge")
    if outcome.kind == "cancelled" or not (outcome.text or "").strip():
        return None
    return deliver(outcome.text, redacted, cfg, tts, dialogue_memory, quiet)
