"""Per-mode bridge settings, read from the ``<mode>_*`` configuration keys."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..memory import activity_runtime


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
    # Shared by both modes (``activity_log_share_with_cloud``); see ``activity_runtime.shared_with_cloud``.
    share_activity_log: bool = False


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
        share_activity_log=activity_runtime.shared_with_cloud(cfg),
    )
