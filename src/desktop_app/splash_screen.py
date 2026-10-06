"""
🚀 Jarvis Splash Screen

The startup splash: the live orb (the same widget the face window shows) in its
thinking look, with a status line underneath that reports initialisation progress.
"""

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QApplication
from PyQt6.QtGui import QPainter, QPen, QColor, QBrush, QFont
from PyQt6.QtCore import Qt, pyqtSignal

from desktop_app.orb_widget import OrbState, OrbWidget
from desktop_app.themes import HUD_COLORS, SPLASH_STATUS_STYLESHEET


class SplashScreen(QWidget):
    """Splash screen shown during application startup."""

    # Signal emitted when splash should close
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()

        # Frameless, always on top, tool window (no taskbar entry)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setFixedSize(320, 360)
        self._setup_ui()
        self._center_on_screen()

    def _setup_ui(self):
        """Set up the UI components."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # The live orb, held in its thinking look while the app starts.
        self._orb = OrbWidget()
        self._orb.setCursor(Qt.CursorShape.ArrowCursor)
        self._orb.set_state_override(OrbState.THINKING)
        self._orb.set_caption("starting up")
        layout.addWidget(self._orb, stretch=1)

        # Status label
        self._status_label = QLabel("Initializing...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_font = QFont()
        status_font.setPointSize(11)
        self._status_label.setFont(status_font)
        self._status_label.setStyleSheet(SPLASH_STATUS_STYLESHEET)
        layout.addWidget(self._status_label)

    def _center_on_screen(self):
        """Center the splash screen on the primary display."""
        screen = QApplication.primaryScreen()
        if screen:
            screen_geometry = screen.availableGeometry()
            x = (screen_geometry.width() - self.width()) // 2 + screen_geometry.x()
            y = (screen_geometry.height() - self.height()) // 2 + screen_geometry.y()
            self.move(x, y)

    def paintEvent(self, event):
        """Draw the splash background."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Rounded HUD panel behind the orb, edged with a faint cyan hairline
        bg_color = QColor(HUD_COLORS["bg_primary"])
        edge = QColor(HUD_COLORS["accent_primary"])
        edge.setAlphaF(0.3)
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(edge, 1))

        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)

    def set_status(self, status: str):
        """Update the status message."""
        self._status_label.setText(status)
        # Process events to ensure the UI updates
        QApplication.processEvents()

    def close_splash(self):
        """Close the splash screen gracefully."""
        self.finished.emit()
        self.close()
