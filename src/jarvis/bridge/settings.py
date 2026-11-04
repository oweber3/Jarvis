"""Per-mode bridge settings, read from the ``<mode>_*`` configuration keys."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable

from .tools import CLOUD_GATED_TOOLS


@dataclass(frozen=True)
class BridgeSettings:
    timeout_sec: float = 90.0
    queue_limit: int = 1
    max_tool_calls: int = 8
    share_recent_dialogue: bool = True
    recent_dialogue_messages: int = 6
    share_desktop_referents: bool = True
    share_foreground_window: bool = True
    share_long_term_memory: bool = False
    # Timers, reminders, lists and notes: offered to the mode independently of long-term memory.
    share_lists_reminders: bool = True
    # Shared by both modes: the opt-in private sources (the activity log, the user's documents, a calendar
    # or email) that are turned on and shared with the cloud now. A private turn may be part of the shared
    # recent dialogue only when every source it quoted is one of them; see ``share_private_turn``.
    shared_private_sources: FrozenSet[str] = frozenset()


def bridge_settings(cfg: Any, mode: str) -> BridgeSettings:
    """The bounds and sharing switches of ``mode`` (``codex`` or ``claude``)."""
    default = BridgeSettings()

    def get(name: str) -> Any:
        return getattr(cfg, f"{mode}_{name}", getattr(default, name))

    return BridgeSettings(
        timeout_sec=float(get("timeout_sec")),
        queue_limit=int(get("queue_limit")),
        max_tool_calls=int(get("max_tool_calls")),
        share_recent_dialogue=bool(get("share_recent_dialogue")),
        recent_dialogue_messages=max(0, int(get("recent_dialogue_messages"))),
        share_desktop_referents=bool(get("share_desktop_referents")),
        share_foreground_window=bool(get("share_foreground_window")),
        share_long_term_memory=bool(get("share_long_term_memory")),
        share_lists_reminders=bool(get("share_lists_reminders")),
        shared_private_sources=shared_private_sources(cfg),
    )


def shared_private_sources(cfg: Any) -> FrozenSet[str]:
    """The opt-in private sources (``bridge.tools.CLOUD_GATED_TOOLS``) that are turned on and shared with
    Codex and Claude now."""
    return frozenset(name for name, source in CLOUD_GATED_TOOLS.items()
                     if source.enabled(cfg) is True and source.shared(cfg) is True)


def share_private_turn(settings: BridgeSettings, sources: Iterable[str]) -> bool:
    """Whether a private turn that quoted ``sources`` may reach the cloud model: only when it records at
    least one source and every one is turned on and shared now. An unknown source is never shared."""
    recorded = frozenset(sources)
    return bool(recorded) and recorded <= settings.shared_private_sources
