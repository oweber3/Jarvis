"""A small badge naming the active cloud reply mode. Hidden in local mode.

Shown on the orb face and in the chat window so it is always visible when requests leave the PC.
Its look and its cloud line icon come from the shared theme (``themes.py``).
"""
from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QWidget

from desktop_app.themes import MODE_BADGE_STYLESHEET, icon_file
from jarvis.bridge.modes import LABELS, LOCAL


class ModeBadge(QLabel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("modeBadge")
        self.setStyleSheet(MODE_BADGE_STYLESHEET)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_mode(LOCAL)

    def set_mode(self, mode: str) -> None:
        if mode == LOCAL or mode not in LABELS:
            self.hide()
            return
        cloud = icon_file("cloud", "accent_secondary")
        self.setText(f'<img src="{cloud}" width="13" height="13" style="vertical-align: middle"> '
                     f'{escape(LABELS[mode])}')
        self.setAccessibleName(f"Cloud reply mode: {LABELS[mode]}")
        self.setToolTip(f"Replies come from {LABELS[mode]} in the cloud. Say \"go local\" to switch back.")
        self.adjustSize()
        self.show()
