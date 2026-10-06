"""The tray's reply-mode submenu and the reply-mode IPC lines (see ``bridge/bridge.spec.md``).

One radio item per mode; a cloud mode that is not allowed in Settings is disabled. Choosing an item
only requests the switch: the checked item always follows the daemon's reported state, so a refused
switch never shows as active.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QMenu

from desktop_app.themes import apply_theme, line_icon
from jarvis.bridge.modes import LABELS, LOCAL, MODES

# Line icons: the PC for local replies, a cloud for the cloud modes.
_ICONS = {"local": "home", "codex": "cloud", "claude": "cloud"}


def request_line(mode: str) -> str:
    """The stdin line asking a subprocess daemon to switch to ``mode``."""
    from jarvis.daemon import REPLY_MODE_IPC_PREFIX
    return REPLY_MODE_IPC_PREFIX + json.dumps({"mode": mode})


def parse_state_line(line: str) -> Optional[Tuple[str, List[str]]]:
    """``(mode, allowed modes)`` from a daemon state event, or None for anything else."""
    from jarvis.daemon import REPLY_MODE_EVENT_PREFIX
    if not line.startswith(REPLY_MODE_EVENT_PREFIX):
        return None
    try:
        data = json.loads(line[len(REPLY_MODE_EVENT_PREFIX):]).get("data") or {}
    except (ValueError, AttributeError):
        return None
    mode, enabled = data.get("mode"), data.get("enabled")
    if mode not in MODES or not isinstance(enabled, list):
        return None
    return mode, [m for m in enabled if m in MODES]


def configured_state() -> Tuple[str, List[str]]:
    """The start-up mode and allowed modes from Settings, for when no daemon is running."""
    from jarvis.bridge.modes import enabled_modes
    from jarvis.config import load_settings
    cfg = load_settings()
    allowed = enabled_modes(cfg)
    return (cfg.reply_mode if cfg.reply_mode in allowed else LOCAL), allowed


class ReplyModeMenu(QObject):
    """``Reply Mode`` submenu. Emits ``mode_requested`` when the user picks an allowed mode."""

    mode_requested = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.menu = QMenu()
        apply_theme(self.menu)
        self.menu.setIcon(line_icon("switch"))
        self._group = QActionGroup(self.menu)
        self._group.setExclusive(True)
        self._actions: Dict[str, QAction] = {}
        self._mode = LOCAL
        for mode in MODES:
            action = QAction(self.menu)
            action.setIcon(line_icon(_ICONS[mode]))
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, m=mode: self._chosen(m))
            self._group.addAction(action)
            self.menu.addAction(action)
            self._actions[mode] = action
        self.set_state(LOCAL, [LOCAL])

    def actions_by_mode(self) -> Dict[str, QAction]:
        return dict(self._actions)

    def set_state(self, mode: str, enabled: List[str]) -> None:
        """Show ``mode`` as active and allow choosing only ``enabled`` modes."""
        self._mode = mode if mode in MODES else LOCAL
        for name, action in self._actions.items():
            allowed = name in enabled
            suffix = "" if allowed else " (allow in Settings)"
            action.setText(f"{LABELS[name]}{suffix}")
            action.setEnabled(allowed)
            action.setChecked(name == self._mode)
        self.menu.setTitle(f"Reply Mode: {LABELS[self._mode]}")

    def _chosen(self, mode: str) -> None:
        # Keep showing the active mode until the daemon confirms the switch.
        self._actions[self._mode].setChecked(True)
        if mode != self._mode:
            self.mode_requested.emit(mode)
