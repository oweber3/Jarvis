"""Extension settings pages in the Settings window (``extensions/extensions.spec.md``, Settings)."""
import json
import textwrap

import pytest

pytestmark = pytest.mark.unit

EXTENSION = """
    from jarvis.extensions import SettingField

    def register(api):
        api.add_settings("Robot Head", [
            SettingField("robot_host", "Robot Address", "Where the robot is.", "str", "", nullable=True),
            SettingField("robot_level", "Level", "How lively.", "int", 3, min=0, max=10),
            SettingField("robot_blink", "Blinking", "Blink now and then.", "bool", True),
        ])
"""


@pytest.fixture
def open_window(qapp, tmp_path, monkeypatch):
    folder = tmp_path / "extensions" / "robot"
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(textwrap.dedent(EXTENSION), encoding="utf-8")
    cfg = tmp_path / "config.json"
    monkeypatch.setattr("desktop_app.settings_window.default_config_path", lambda: cfg)
    monkeypatch.setattr("desktop_app.settings_window.QMessageBox.information", lambda *a, **k: None)
    windows = []

    def make(values):
        cfg.write_text(json.dumps({"extensions_dir": str(tmp_path / "extensions"), **values}))
        from desktop_app.settings_window import SettingsWindow
        win = SettingsWindow()
        windows.append(win)
        return win, cfg

    yield make
    for win in windows:
        win.close()


def sidebar_labels(win):
    return [win._sidebar.item(i).text() for i in range(win._sidebar.count())]


def test_an_enabled_extension_gets_its_own_page_after_the_built_in_ones(open_window):
    from desktop_app.settings_window import CATEGORIES

    win, _ = open_window({"extensions_enabled": ["robot"], "robot_level": 7})
    assert sidebar_labels(win) == [label for _key, label in CATEGORIES] + ["Robot Head"]
    assert win._widgets["robot_level"].value() == 7
    assert win._widgets["robot_blink"].isChecked() is True


def test_extension_settings_save_like_built_in_ones(open_window):
    win, cfg = open_window({"extensions_enabled": ["robot"], "robot_level": 7})
    win._widgets["robot_level"].setValue(3)  # the default
    win._widgets["robot_blink"].setChecked(False)
    win._widgets["robot_host"].setText("10.0.0.9")
    win._on_save()
    saved = json.loads(cfg.read_text())
    assert "robot_level" not in saved
    assert saved["robot_blink"] is False and saved["robot_host"] == "10.0.0.9"


def test_a_disabled_extension_has_no_page_and_keeps_its_values(open_window):
    from desktop_app.settings_window import CATEGORIES

    win, cfg = open_window({"robot_level": 7})
    assert sidebar_labels(win) == [label for _key, label in CATEGORIES]
    win._widgets["windows_tools_enabled"].setChecked(False)
    win._on_save()
    assert json.loads(cfg.read_text())["robot_level"] == 7


def test_a_broken_extension_leaves_the_window_working(open_window, tmp_path):
    (tmp_path / "extensions" / "robot" / "__init__.py").write_text("raise RuntimeError('broken')\n")
    from desktop_app.settings_window import CATEGORIES

    win, _ = open_window({"extensions_enabled": ["robot"]})
    assert sidebar_labels(win) == [label for _key, label in CATEGORIES]


def test_register_sees_the_whole_config_in_the_settings_window(open_window, tmp_path):
    (tmp_path / "extensions" / "robot" / "__init__.py").write_text(textwrap.dedent(EXTENSION) + textwrap.dedent("""
        _register = register

        def register(api):
            assert api.config.tts_engine == "piper"
            _register(api)
    """), encoding="utf-8")
    win, _ = open_window({"extensions_enabled": ["robot"]})
    assert "robot_level" in win._widgets


def test_a_key_two_extensions_declare_is_shown_once(open_window, tmp_path):
    twin = tmp_path / "extensions" / "twin"
    twin.mkdir()
    (twin / "__init__.py").write_text(textwrap.dedent("""
        from jarvis.extensions import SettingField

        def register(api):
            api.add_settings("Twin", [SettingField("robot_level", "Level", "Again.", "int", 9)])
    """), encoding="utf-8")
    win, cfg = open_window({"extensions_enabled": ["robot", "twin"], "robot_level": 7})
    assert [fm.key for fm in win._fields].count("robot_level") == 1
    win._widgets["robot_level"].setValue(5)
    win._on_save()
    assert json.loads(cfg.read_text())["robot_level"] == 5
