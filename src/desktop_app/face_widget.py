"""
Jarvis face window: hosts the reactive orb visual and owns the daemon state channel.

- ``JarvisState`` / ``JarvisStateManager`` / ``get_jarvis_state``: the file-backed,
  cross-process state the daemon writes and the visual reads (dev mode runs the
  daemon as a subprocess, bundled mode as a thread; a file works for both).
- ``FaceWindow``: the floating window. The visual itself lives in
  ``desktop_app.orb_widget`` (``OrbWidget``).
"""

from __future__ import annotations
import os
import tempfile
import threading
from enum import Enum
from typing import Optional
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QApplication
from PyQt6.QtCore import Qt, pyqtSignal, QObject

from desktop_app.mode_badge import ModeBadge
from desktop_app.themes import FACE_STYLESHEET
from desktop_app.orb_widget import OrbWidget, OrbState

class JarvisState(Enum):
    """Overall Jarvis state for face animation."""
    ASLEEP = "asleep"          # Daemon not started yet
    IDLE = "idle"              # Awake and ready, waiting for wake word
    LISTENING = "listening"    # Actively listening (collecting or hot window)
    THINKING = "thinking"      # Processing query
    SPEAKING = "speaking"      # Speaking response
    DICTATING = "dictating"    # Hold-to-dictate recording active
    DICTATION_PROCESSING = "dictation_processing"  # Transcribing & pasting captured dictation


# Global Jarvis state - allows daemon to signal overall state to face widget
# Uses a file-based approach to work across processes (dev mode runs daemon as subprocess)
def _get_jarvis_state_file() -> str:
    """Get the path to the Jarvis state file."""
    return os.path.join(tempfile.gettempdir(), "jarvis_state")


class JarvisStateManager(QObject):
    """Global singleton for Jarvis state management.

    Uses a file-based approach to communicate across processes:
    - In dev mode, daemon runs as subprocess (different process)
    - In bundled mode, daemon runs as QThread (same process)
    - File-based state works in both cases

    Note: Singleton pattern uses module-level instance instead of __new__
    because PyQt6 QObject doesn't support __new__ override properly.
    """
    state_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._state = JarvisState.ASLEEP  # Start asleep
        self._state_lock = threading.Lock()
        self._state_file = _get_jarvis_state_file()
        # Always start fresh in ASLEEP state on app launch
        # (state file is for cross-process communication during a session,
        # not for persisting state across app restarts)
        self._write_state(JarvisState.ASLEEP)

    @property
    def state(self) -> JarvisState:
        """Read current state (checks file for cross-process communication)."""
        # First check file (for cross-process), then fall back to memory
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r') as f:
                    content = f.read().strip()
                    return JarvisState(content)
        except (ValueError, OSError):
            # Invalid content or read error - fall back to in-memory state
            pass

        with self._state_lock:
            return self._state

    def _write_state(self, state: JarvisState) -> None:
        """Write state to file for cross-process communication."""
        try:
            with open(self._state_file, 'w') as f:
                f.write(state.value)
        except OSError:
            # File write failed - state won't be shared across processes
            pass

    def set_state(self, state: JarvisState) -> None:
        """Set the Jarvis state (thread-safe, cross-process)."""
        with self._state_lock:
            self._state = state

        # Write to file for cross-process communication
        self._write_state(state)

        # Emit signal for same-process listeners
        try:
            self.state_changed.emit(state.value)
        except RuntimeError:
            # If Qt event loop isn't running, just update the flag
            pass


# Module-level singleton instance
_jarvis_state_instance: Optional[JarvisStateManager] = None
_jarvis_state_lock = threading.Lock()


def get_jarvis_state() -> JarvisStateManager:
    """Get the global Jarvis state singleton."""
    global _jarvis_state_instance
    with _jarvis_state_lock:
        if _jarvis_state_instance is None:
            _jarvis_state_instance = JarvisStateManager()
        return _jarvis_state_instance


class FaceWindow(QWidget):
    """A standalone window containing the Jarvis orb."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Jarvis")
        self.setMinimumSize(300, 340)
        self.resize(380, 440)

        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setStyleSheet(FACE_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.orb = OrbWidget()
        layout.addWidget(self.orb)

        # Floats over the orb's top edge; visible only in a cloud reply mode.
        self.mode_badge = ModeBadge(self)

        self._position_on_right()

    def set_reply_mode(self, mode: str) -> None:
        """Show the active reply mode (badge hidden in local mode)."""
        self.mode_badge.set_mode(mode)
        self._place_badge()

    def _place_badge(self) -> None:
        self.mode_badge.move((self.width() - self.mode_badge.width()) // 2, 12)
        self.mode_badge.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_badge()

    def _position_on_right(self):
        """Position the window on the right side of the screen, vertically centered."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return

        screen_geometry = screen.availableGeometry()
        window_width = self.width()
        window_height = self.height()

        margin = 20
        x = screen_geometry.right() - window_width - margin
        y = screen_geometry.top() + (screen_geometry.height() - window_height) // 2

        self.move(x, y)

    def set_state_override(self, state: Optional[OrbState]) -> None:
        """Force a visual state such as MUTED or ERROR; ``None`` follows the daemon."""
        self.orb.set_state_override(state)
