"""File deletion switch: off by default, and while off no request can delete a file."""
import json
import os
from types import SimpleNamespace

import pytest

from jarvis.config import get_default_config, load_settings
from jarvis.tools.base import ToolContext
from jarvis.tools.builtin.local_files import LocalFilesTool
from jarvis.tools.confirmation import (
    ConfirmationRequest, SafetyTier, get_confirmation_store, set_dialog_callback,
)
from jarvis.tools.registry import run_tool_with_retries

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_store():
    store = get_confirmation_store()
    store.clear_pending()
    set_dialog_callback(None)
    yield
    store.clear_pending()
    set_dialog_callback(None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    real = os.path.expanduser

    def fake(p):
        if isinstance(p, str) and (p == "~" or p.startswith(("~/", "~\\"))):
            return str(tmp_path / p[2:]) if len(p) > 1 else str(tmp_path)
        return real(p)

    import jarvis.tools.builtin.local_files as lf
    import jarvis.tools.confirmation as conf
    for module in (os.path, lf.os.path, conf.os.path):
        monkeypatch.setattr(module, "expanduser", fake)
    return tmp_path


def cfg(**values):
    return SimpleNamespace(windows_tools_enabled=True, voice_debug=False, **values)


def delete(config, name="keep.txt"):
    return run_tool_with_retries(db=None, cfg=config, tool_name="localFiles",
                                 tool_args={"operation": "delete", "path": f"~/{name}"},
                                 system_prompt="", original_prompt="", redacted_text="")


class TestSetting:
    def write(self, tmp_path, monkeypatch, values):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(values))
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(path))

    def test_off_by_default(self, tmp_path, monkeypatch):
        self.write(tmp_path, monkeypatch, {})
        assert load_settings().file_delete_enabled is get_default_config()["file_delete_enabled"] is False

    @pytest.mark.parametrize("value", ["true", 1, "yes"])
    def test_only_a_real_true_turns_it_on(self, tmp_path, monkeypatch, value):
        self.write(tmp_path, monkeypatch, {"file_delete_enabled": value})
        assert load_settings().file_delete_enabled is False

    def test_true_turns_it_on(self, tmp_path, monkeypatch):
        self.write(tmp_path, monkeypatch, {"file_delete_enabled": True})
        assert load_settings().file_delete_enabled is True

    def test_the_switch_is_in_settings(self):
        from desktop_app.settings_window import FIELD_METADATA
        assert "file_delete_enabled" in {getattr(field, "key", None) for field in FIELD_METADATA}


class TestOff:
    @pytest.mark.parametrize("config", [cfg(), cfg(file_delete_enabled=False), cfg(file_delete_enabled="true")])
    def test_delete_is_refused_without_asking(self, home, config):
        target = home / "keep.txt"
        target.write_text("data")
        result = delete(config)
        assert result.success is False
        assert target.exists()
        # Refused outright: nothing waits for a "yes" that could delete it.
        assert not get_confirmation_store().has_pending()
        assert "turned off" in (result.error_message or "").lower()

    def test_an_approved_delete_still_does_not_run_once_the_switch_is_off(self, home):
        """A confirmation given before the switch went off cannot delete anything."""
        target = home / "keep.txt"
        target.write_text("data")
        context = ToolContext(db=None, cfg=cfg(file_delete_enabled=False), system_prompt="",
                              original_prompt="", redacted_text="", max_retries=1, user_print=lambda *a: None)
        result = LocalFilesTool().run({"operation": "delete", "path": "~/keep.txt"}, context)
        assert result.success is False and target.exists()

    def test_other_file_operations_still_work(self, home):
        result = run_tool_with_retries(db=None, cfg=cfg(), tool_name="localFiles",
                                       tool_args={"operation": "write", "path": "~/new.txt", "content": "hi"},
                                       system_prompt="", original_prompt="", redacted_text="")
        assert result.success is True and (home / "new.txt").read_text() == "hi"


class TestOn:
    def test_delete_still_asks_first(self, home):
        target = home / "gone.txt"
        target.write_text("data")
        result = delete(cfg(file_delete_enabled=True), "gone.txt")
        assert result.success is False and target.exists()
        pending = get_confirmation_store().get_pending()
        assert pending is not None and pending.request.tier == SafetyTier.CONFIRM_VOICE

    def test_confirmed_delete_runs(self, home):
        target = home / "gone.txt"
        target.write_text("data")
        config = cfg(file_delete_enabled=True)
        context = ToolContext(db=None, cfg=config, system_prompt="", original_prompt="", redacted_text="",
                              max_retries=1, user_print=lambda *a: None)
        assert LocalFilesTool().run({"operation": "delete", "path": "~/gone.txt"}, context).success is True
        assert not target.exists()
