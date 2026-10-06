"""The tray's Phone Access dialog: turn phone access on, pair a phone with a code, manage paired phones.

It works on the same device files as the daemon (``jarvis.remote.pairing``), so pairing and removal reach a
daemon running in this process or as a subprocess. See ``jarvis/remote/remote.spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.config import update_config_values
from jarvis.debug import debug_log
from jarvis.remote.pairing import DeviceStore
from jarvis.remote.runtime import devices_directory, phone_links

from .themes import apply_theme, divider, eyebrow, hud_heading, set_role


@dataclass
class DeviceRow:
    id: str
    name: str
    label: QLabel
    widget: QWidget


def _when(stamp: float) -> str:
    return datetime.fromtimestamp(stamp).strftime("%d %b %Y, %H:%M") if stamp else "never"


def _plain(text: str, *, muted: bool = False, selectable: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if muted:
        label.setObjectName("muted")
    if selectable:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class PhoneAccessDialog(QDialog):
    """Pairing and paired phones. ``cfg`` defaults to the saved settings."""

    def __init__(self, cfg=None, parent=None) -> None:
        super().__init__(parent)
        if cfg is None:
            from jarvis.config import load_settings
            cfg = load_settings()
        self._cfg = cfg
        self._store = DeviceStore(devices_directory(cfg))
        self._pairing_devices: Optional[int] = None
        self.device_rows: List[DeviceRow] = []
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._build()
        self.refresh()

    # -- layout --------------------------------------------------------------------------

    def _card(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("inset")
        return frame

    def _build(self) -> None:
        self.setWindowTitle("Phone Access")
        self.setMinimumWidth(460)
        apply_theme(self)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        heading, _, _ = hud_heading("Remote", "Phone Access")
        layout.addWidget(heading)
        layout.addWidget(divider())

        self.status_label = _plain("")
        self.status_label.setObjectName("body")
        layout.addWidget(self.status_label)
        self.turn_on_button = QPushButton("Turn on phone access")
        self.turn_on_button.setObjectName("primary")
        self.turn_on_button.clicked.connect(self.turn_on)
        layout.addWidget(self.turn_on_button)

        links = self._card()
        links_layout = QVBoxLayout(links)
        links_layout.setContentsMargins(14, 12, 14, 12)
        links_layout.addWidget(eyebrow("Open one of these in your phone's browser"))
        self.links_label = _plain("", selectable=True)
        self.links_label.setObjectName("link")
        links_layout.addWidget(self.links_label)
        links_layout.addWidget(_plain("The phone must be on your home network or your VPN (such as Tailscale).",
                                      muted=True))
        layout.addWidget(links)

        pairing = self._card()
        pairing_layout = QVBoxLayout(pairing)
        pairing_layout.setContentsMargins(14, 12, 14, 12)
        self.pair_button = QPushButton("Pair a phone")
        self.pair_button.clicked.connect(self.start_pairing)
        pairing_layout.addWidget(self.pair_button)
        self.code_label = QLabel("")
        self.code_label.setObjectName("code")
        self.code_label.setTextFormat(Qt.TextFormat.PlainText)
        self.code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_font = self.code_label.font()
        code_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
        self.code_label.setFont(code_font)
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.code_label.hide()
        pairing_layout.addWidget(self.code_label)
        self.code_hint = _plain("", muted=True)
        self.code_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_hint.hide()
        pairing_layout.addWidget(self.code_hint)
        layout.addWidget(pairing)

        layout.addWidget(eyebrow("Paired phones"))
        self.devices_box = QVBoxLayout()
        self.devices_box.setSpacing(8)
        layout.addLayout(self.devices_box)
        self.no_devices = _plain("No phones are paired yet.", muted=True)
        self.devices_box.addWidget(self.no_devices)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def event(self, event) -> bool:
        handled = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self._fit_minimum_height()
        return handled

    def _fit_minimum_height(self) -> None:
        """Never shrink below the height the wrapped text needs at the narrowest width.

        A layout's minimum size ignores word-wrapped labels growing taller as the window narrows.
        """
        needed = self.heightForWidth(self.minimumWidth())
        if needed > 0 and needed != self.minimumHeight():
            self.setMinimumHeight(needed)

    # -- state ---------------------------------------------------------------------------

    def refresh(self) -> None:
        enabled = getattr(self._cfg, "remote_access_enabled", False) is True
        set_role(self.status_label, "body")
        if enabled:
            self.status_label.setText("Phone access is on. Pair a phone, then open the link on it.")
        else:
            self.status_label.setText("Phone access is off. Turn it on, then restart Jarvis.")
        self.turn_on_button.setHidden(enabled)
        self.links_label.setText("\n".join(phone_links(int(self._cfg.remote_access_port))))
        self._render_devices()

    def _render_devices(self) -> None:
        for row in self.device_rows:
            self.devices_box.removeWidget(row.widget)
            row.widget.deleteLater()
        self.device_rows = []
        devices = self._store.devices()
        self.no_devices.setVisible(not devices)
        for device in devices:
            card = self._card()
            line = QHBoxLayout(card)
            text = QVBoxLayout()
            line.setContentsMargins(14, 10, 10, 10)
            name = _plain(device.name)
            name.setObjectName("heading")
            text.addWidget(name)
            text.addWidget(_plain(f"Added {_when(device.created_at)} · last seen {_when(device.last_seen)}",
                                  muted=True))
            line.addLayout(text, 1)
            remove = QPushButton("Remove")
            remove.setObjectName("danger")
            remove.setProperty("compact", True)
            remove.clicked.connect(lambda _checked=False, device_id=device.id: self.remove_device(device_id))
            line.addWidget(remove)
            self.devices_box.addWidget(card)
            self.device_rows.append(DeviceRow(id=device.id, name=device.name, label=name, widget=card))

    # -- actions -------------------------------------------------------------------------

    def turn_on(self) -> None:
        if update_config_values({"remote_access_enabled": True}):
            self._cfg.remote_access_enabled = True
            self.refresh()
            self.status_label.setText("Phone access is turned on. Restart Jarvis to start it.")
            set_role(self.status_label, "status-success")
            debug_log("phone access turned on from the tray", "desktop")
        else:
            self.status_label.setText("The setting could not be saved. Try Settings > Phone Access.")
            set_role(self.status_label, "status-warning")

    def start_pairing(self) -> None:
        code = self._store.start_pairing()
        self._pairing_devices = len(self._store.devices())
        self.code_label.setText(f"{code[:3]} {code[3:]}")
        self.code_label.show()
        self.code_hint.show()
        self.pair_button.setText("New code")
        self._tick()
        self._timer.start()

    def _tick(self) -> None:
        if not self._store.pairing_active():
            self._timer.stop()
            self.code_label.hide()
            self.pair_button.setText("Pair a phone")
            paired = self._pairing_devices is not None and len(self._store.devices()) > self._pairing_devices
            self.code_hint.setText("Phone paired." if paired else "That code has ended. Make a new one.")
            self._pairing_devices = None
            self._render_devices()
            return
        left = int(self._store.pairing_seconds_left())
        self.code_hint.setText(f"Enter this code on the phone · {left // 60}:{left % 60:02d} left · works once")

    def remove_device(self, device_id: str, ask: bool = True) -> None:
        row = next((r for r in self.device_rows if r.id == device_id), None)
        if row is None:
            return
        if ask:
            answer = QMessageBox.question(self, "Remove phone",
                                          f"Remove {row.name}? It will need a new code to connect again.",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._store.revoke(device_id)
        self._render_devices()

    def done(self, result: int) -> None:
        self._timer.stop()
        super().done(result)
