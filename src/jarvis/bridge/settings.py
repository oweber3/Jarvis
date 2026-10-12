"""Per-mode bridge settings, read from the ``<mode>_*`` configuration keys."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    # Shared by both modes: whether private turns (they quote the activity log, the user's documents, a
    # calendar or email) may be part of the shared recent dialogue; see ``share_private_turns``.
    share_private_turns: bool = False


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
        share_private_turns=share_private_turns(cfg),
    )


def share_private_turns(cfg: Any) -> bool:
    """Private turns may reach a cloud model only when at least one opt-in source is shared and every
    source that is set up is shared (``bridge.tools.CLOUD_GATED_TOOLS``): a private turn does not record
    which source it quoted."""
    sources = [(source.set_up(cfg), source.shared(cfg)) for source in CLOUD_GATED_TOOLS.values()]
    return any(shared for _, shared in sources) and all(shared or not set_up for set_up, shared in sources)
