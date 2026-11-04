"""Settings > Smart Home: the page, test-before-save, secrets in the credential store and the device list.
Qt widgets run offscreen against a loopback fake Home Assistant."""
import json
import time

import pytest

from fake_home_assistant import FakeHomeAssistant, entity
from jarvis import credentials
from jarvis.smarthome.service import HA_TOKEN_KEY


def inline(job, done):
    """Run settings jobs at once, so tests need no event loop."""
    done(job())


@pytest.fixture
def ha():
    server = FakeHomeAssistant([entity("light.lamp", "Lamp", area="Den"), entity("switch.plug", "Plug")],
                               token="tok")
    yield server
    server.close()


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"_config_version": 6, "db_path": str(tmp_path / "j.db")}), encoding="utf-8")
    monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
    warnings = []
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.warning", lambda *a, **k: warnings.append(a[2]))
    from desktop_app.settings_window import SettingsWindow
    win = SettingsWindow()
    win.smart_home_page._run = inline
    win.cfg_path, win.warnings = cfg, warnings
    yield win
    win.close()


def saved(win):
    return json.loads(win.cfg_path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_the_page_is_listed_and_its_fields_follow_the_switch(window):
    assert "Smart Home" in window.visible_pages()
    assert not window.is_field_shown("smarthome_ha_url")
    window._widgets["smarthome_enabled"].setChecked(True)
    assert window.is_field_shown("smarthome_ha_url") and window.is_field_shown("smarthome_share_with_cloud")


@pytest.mark.unit
def test_an_untested_address_is_not_saved(window, ha):
    window._widgets["smarthome_enabled"].setChecked(True)
    window._widgets["smarthome_ha_url"].setText(ha.url)
    window._on_save()
    assert window.warnings and "Test the Home Assistant connection" in window.warnings[-1]
    assert "smarthome_ha_url" not in saved(window)


@pytest.mark.unit
def test_a_passing_test_stores_the_token_and_lets_the_address_save(window, ha):
    page = window.smart_home_page
    window._widgets["smarthome_enabled"].setChecked(True)
    window._widgets["smarthome_ha_url"].setText(ha.url)
    page.token.setText("tok")
    page.ha_test.click()
    assert "2 exposed devices" in page.ha_result.text()
    assert credentials.get_secret(HA_TOKEN_KEY) == "tok"
    assert page.token.text() == "" and page.token_status.text() == "Saved"
    window._on_save()
    written = saved(window)
    assert written["smarthome_ha_url"] == ha.url and written["smarthome_enabled"] is True
    assert "tok" not in window.cfg_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_a_failing_test_stores_nothing(window, ha):
    page = window.smart_home_page
    window._widgets["smarthome_enabled"].setChecked(True)
    window._widgets["smarthome_ha_url"].setText(ha.url)
    page.token.setText("wrong")
    page.ha_test.click()
    assert "create a new one" in page.ha_result.text()
    assert credentials.get_secret(HA_TOKEN_KEY) == "" and page.token_status.text() == "Not saved"


@pytest.mark.unit
def test_the_device_list_writes_always_confirm_choices(window, ha):
    page = window.smart_home_page
    window._widgets["smarthome_enabled"].setChecked(True)
    window._widgets["smarthome_ha_url"].setText(ha.url)
    page.token.setText("tok")
    page.ha_test.click()
    page.load.click()
    assert page.table.rowCount() == 2
    names = [page.table.item(row, 0).text() for row in range(2)]
    lamp = names.index("Lamp")
    assert not page.table.cellWidget(lamp, 5).isEnabled()   # Home Assistant devices are chosen with Expose
    page.table.cellWidget(lamp, 6).setChecked(True)
    window._on_save()
    assert saved(window)["smarthome_confirm_devices"] == ["ha:light.lamp"]
