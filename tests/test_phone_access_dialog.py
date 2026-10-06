"""The tray's Phone Access dialog: turn it on, pair a phone with a code, see and remove paired phones."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jarvis.remote.pairing import DeviceStore  # noqa: E402


def cfg(tmp_path, enabled=True, port=8765):
    return SimpleNamespace(remote_access_enabled=enabled, remote_access_port=port,
                           db_path=str(tmp_path / "jarvis.db"))


def open_dialog(tmp_path, **kwargs):
    from desktop_app.phone_access_dialog import PhoneAccessDialog
    return PhoneAccessDialog(cfg=cfg(tmp_path, **kwargs))


@pytest.mark.unit
class TestPhoneAccessDialog:
    def test_a_pairing_code_from_the_dialog_pairs_a_phone(self, qapp, tmp_path):
        dialog = open_dialog(tmp_path)
        dialog.pair_button.click()
        code = dialog.code_label.text().replace(" ", "")
        assert len(code) == 6 and code.isdigit()
        assert DeviceStore(tmp_path).complete_pairing(code, "Alex's iPhone") is not None
        dialog.refresh()
        assert [row.name for row in dialog.device_rows] == ["Alex's iPhone"]

    def test_removing_a_phone_revokes_it(self, qapp, tmp_path):
        store = DeviceStore(tmp_path)
        result = store.complete_pairing(store.start_pairing(), "tablet")
        dialog = open_dialog(tmp_path)
        (row,) = dialog.device_rows
        dialog.remove_device(row.id, ask=False)
        assert store.authenticate(result.token) is None
        assert dialog.device_rows == []

    def test_the_links_use_the_configured_port(self, qapp, tmp_path):
        dialog = open_dialog(tmp_path, port=9123)
        assert ":9123/" in dialog.links_label.text()

    def test_when_off_the_dialog_offers_to_turn_it_on(self, qapp, tmp_path, monkeypatch):
        written = []
        monkeypatch.setattr("desktop_app.phone_access_dialog.update_config_values",
                            lambda values: written.append(values) or True)
        dialog = open_dialog(tmp_path, enabled=False)
        assert not dialog.turn_on_button.isHidden()
        dialog.turn_on_button.click()
        assert written == [{"remote_access_enabled": True}]
        assert "restart" in dialog.status_label.text().lower()

    def test_when_on_there_is_nothing_to_turn_on(self, qapp, tmp_path):
        assert open_dialog(tmp_path).turn_on_button.isHidden()

    def test_device_names_are_shown_as_plain_text(self, qapp, tmp_path):
        from PyQt6.QtCore import Qt
        store = DeviceStore(tmp_path)
        store.complete_pairing(store.start_pairing(), "<b>phone</b>")
        dialog = open_dialog(tmp_path)
        assert dialog.device_rows[0].label.textFormat() == Qt.TextFormat.PlainText


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("pairing", [False, True])
def test_shrinking_the_dialog_never_squeezes_wrapped_text(qapp, tmp_path, monkeypatch, enabled, pairing):
    """Wrapped lines (status, network note, device rows) keep the height they need at the smallest size."""
    import sys
    from pathlib import Path
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QLabel
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import ui_windows
    ui_windows.load_fonts()
    monkeypatch.setattr("desktop_app.phone_access_dialog.phone_links",
                        lambda port: [f"http://192.168.1.20:{port}/", f"http://my-pc:{port}/"])
    store = DeviceStore(tmp_path)
    store.complete_pairing(store.start_pairing(), "A phone with a fairly long descriptive name")
    dialog = open_dialog(tmp_path, enabled=enabled)
    if pairing:
        dialog.pair_button.click()
    dialog.show()
    QTest.qWait(30)
    dialog.resize(1, 1)
    QTest.qWait(30)
    squeezed = [label.text()[:40] for label in dialog.findChildren(QLabel)
                if label.isVisible() and label.wordWrap() and label.text()
                and label.height() < label.heightForWidth(label.width())]
    assert squeezed == []
    dialog.close()
