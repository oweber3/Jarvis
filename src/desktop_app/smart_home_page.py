"""Settings > Smart Home: the access token and broker password, the Test buttons and the device list.

The page only arranges widgets; testing, saving secrets and reading devices is ``jarvis.smarthome.setup``,
run off the GUI thread. A token or password is saved to Windows Credential Manager only when its test passes,
and Save refuses changed connection settings until a test with those values has passed. See
``settings_window.spec.md`` (Smart Home) and ``smarthome/smarthome.spec.md``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from desktop_app.accounts_page import run_in_thread
from desktop_app.themes import set_role
from jarvis import credentials
from jarvis.debug import debug_log
from jarvis.smarthome import setup
from jarvis.smarthome.service import HA_TOKEN_KEY, MQTT_PASSWORD_KEY

COLUMNS = ("Name", "Room", "Kind", "Source", "State", "Hide", "Always confirm")
PRIVACY_NOTE = ("Jarvis talks only to your own Home Assistant and MQTT broker on your home network. Only devices "
                "you expose to voice assistants in Home Assistant are used.")


def _guide_path() -> Optional[Path]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / setup.GUIDE
        if candidate.is_file():
            return candidate
    return None


class SmartHomePage(QWidget):
    """``get(key)`` reads a field as the window shows it now; ``saved`` is the file's current values."""

    def __init__(self, get: Callable[[str], Any], saved: Dict[str, Any], parent: Optional[QWidget] = None,
                 runner: Callable = run_in_thread):
        super().__init__(parent)
        self._get = get
        self._run = runner
        self.gate = setup.SaveGate(saved={"ha": self._ha_key(saved.get), "mqtt": self._mqtt_key(saved.get)})
        self.hidden: List[str] = list(saved.get("smarthome_mqtt_hidden") or [])
        self.confirm: List[str] = list(saved.get("smarthome_confirm_devices") or [])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        note = QLabel(PRIVACY_NOTE)
        note.setWordWrap(True)
        set_role(note, "hint")
        layout.addWidget(note)

        form = QFormLayout()
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Paste a long-lived access token")
        self.token_status = QLabel()
        self.ha_test = QPushButton("Test connection")
        self.ha_test.clicked.connect(self._test_ha)
        self.ha_result = QLabel()
        self.ha_result.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.token, 1)
        row.addWidget(self.token_status)
        row.addWidget(self.ha_test)
        form.addRow("Home Assistant Token", row)
        form.addRow("", self.ha_result)
        guide = _guide_path()
        self.guide = QLabel(f'<a href="{guide.as_uri()}">Smart home setup guide</a>' if guide
                            else f"Setup guide: {setup.GUIDE}")
        self.guide.setOpenExternalLinks(True)
        self.guide.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        form.addRow("", self.guide)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Broker password, if it has one")
        self.password_status = QLabel()
        self.mqtt_test = QPushButton("Test connection")
        self.mqtt_test.clicked.connect(self._test_mqtt)
        self.mqtt_result = QLabel()
        self.mqtt_result.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.password, 1)
        row.addWidget(self.password_status)
        row.addWidget(self.mqtt_test)
        form.addRow("MQTT Password", row)
        form.addRow("", self.mqtt_result)
        layout.addLayout(form)

        self.load = QPushButton("Show devices")
        self.load.clicked.connect(self._load_devices)
        self.devices_status = QLabel("Shows the devices Jarvis can see with the saved settings.")
        self.devices_status.setWordWrap(True)
        set_role(self.devices_status, "status-muted")
        row = QHBoxLayout()
        row.addWidget(self.load)
        row.addWidget(self.devices_status, 1)
        layout.addLayout(row)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(180)
        self.table.hide()
        layout.addWidget(self.table)
        self._refresh_secret_status()

    # -- values ----------------------------------------------------------------------------------

    @staticmethod
    def _ha_key(get) -> tuple:
        return setup.ha_key(get("smarthome_ha_url") or "", get("smarthome_ha_verify_tls") is not False)

    @staticmethod
    def _mqtt_key(get) -> tuple:
        return setup.mqtt_key(get("smarthome_mqtt_host") or "", get("smarthome_mqtt_port") or 1883,
                              get("smarthome_mqtt_username") or "", get("smarthome_mqtt_tls") is True,
                              get("smarthome_mqtt_discovery_prefix") or "homeassistant")

    def _refresh_secret_status(self) -> None:
        for label, key in ((self.token_status, HA_TOKEN_KEY), (self.password_status, MQTT_PASSWORD_KEY)):
            stored = credentials.has_secret(key)
            label.setText("Saved" if stored else "Not saved")
            set_role(label, "status-success" if stored else "status-muted")

    def save_problem(self) -> Optional[str]:
        """Why the window must not save yet, or None."""
        # Whether or not the feature is on: turning it on later must not skip the test.
        return self.gate.problem(self._ha_key(self._get), self._mqtt_key(self._get))

    def apply(self, config: Dict[str, Any]) -> None:
        """Write the device list's hide and always-confirm choices into the config about to be saved."""
        setup.apply_device_choices(config, self.hidden, self.confirm)

    # -- tests -----------------------------------------------------------------------------------

    def _show(self, label: QLabel, result: setup.TestResult) -> None:
        label.setText(result.message)
        set_role(label, "status-success" if result.ok else "status-error")

    def _test_ha(self) -> None:
        key = self._ha_key(self._get)
        typed = self.token.text()
        self.ha_test.setEnabled(False)
        self.ha_result.setText("Testing...")
        set_role(self.ha_result, "status-muted")

        def done(result: setup.TestResult) -> None:
            self.ha_test.setEnabled(True)
            if result.ok:
                self.gate.passed_with(key)
                self.token.clear()
            self._show(self.ha_result, result)
            self._refresh_secret_status()

        debug_log("settings: Home Assistant test", "settings")
        self._run(lambda: setup.test_home_assistant(key[1], typed, key[2]), done)

    def _test_mqtt(self) -> None:
        key = self._mqtt_key(self._get)
        typed = self.password.text()
        self.mqtt_test.setEnabled(False)
        self.mqtt_result.setText("Testing...")
        set_role(self.mqtt_result, "status-muted")

        def done(result: setup.TestResult) -> None:
            self.mqtt_test.setEnabled(True)
            if result.ok:
                self.gate.passed_with(key)
                self.password.clear()
            self._show(self.mqtt_result, result)
            self._refresh_secret_status()

        debug_log("settings: MQTT test", "settings")
        self._run(lambda: setup.test_mqtt(key[1], key[2], key[3], typed, key[4], key[5]), done)

    # -- devices ---------------------------------------------------------------------------------

    def _load_devices(self) -> None:
        from types import SimpleNamespace
        from jarvis.smarthome import service
        names = ("smarthome_ha_url", "smarthome_ha_verify_tls", "smarthome_mqtt_host", "smarthome_mqtt_port",
                 "smarthome_mqtt_username", "smarthome_mqtt_tls", "smarthome_mqtt_discovery_prefix")
        cfg = SimpleNamespace(**{k: self._get(k) for k in names}, smarthome_enabled=True,
                              smarthome_mqtt_hidden=list(self.hidden))
        self.load.setEnabled(False)
        self.devices_status.setText("Connecting...")

        def job():
            # Always the (rows, problems) pair: a crash here must never reach the Qt slot as something else.
            try:
                return setup.read_devices(service.build(cfg, warn=lambda _message: None))
            except Exception as exc:  # noqa: BLE001 - reported on the page
                debug_log(f"settings: device list failed ({type(exc).__name__})", "settings")
                return [], ["The device list could not be read. Check the settings above and try again."]

        self._run(job, self.show_devices)

    def show_devices(self, outcome) -> None:
        rows, problems = outcome
        self.load.setEnabled(True)
        self.devices_status.setText(" ".join(problems) or f"{len(rows)} devices.")
        set_role(self.devices_status, "status-warning" if problems else "status-muted")
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, text in enumerate((row.name, row.area, row.kind,
                                           "Home Assistant" if row.source == "ha" else "MQTT", row.state)):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)
            hide = QCheckBox()
            hide.setChecked(row.id in self.hidden)
            hide.setEnabled(row.source == "mqtt")
            hide.setToolTip("Home Assistant devices are chosen with Expose in Home Assistant."
                            if row.source != "mqtt" else "Jarvis never lists, names or controls a hidden device.")
            hide.toggled.connect(lambda on, device_id=row.id: self._toggle(self.hidden, device_id, on))
            self.table.setCellWidget(index, 5, hide)
            confirm = QCheckBox()
            confirm.setChecked(row.id in self.confirm)
            confirm.toggled.connect(lambda on, device_id=row.id: self._toggle(self.confirm, device_id, on))
            self.table.setCellWidget(index, 6, confirm)
        self.table.setVisible(bool(rows))

    @staticmethod
    def _toggle(values: List[str], device_id: str, on: bool) -> None:
        if on and device_id not in values:
            values.append(device_id)
        elif not on and device_id in values:
            values.remove(device_id)
