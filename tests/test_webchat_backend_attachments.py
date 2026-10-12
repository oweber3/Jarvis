"""The web chat backend's side of attachments: what the active route can take, the Screenshot button and
the hand-over to the text path. The daemon module's functions are replaced; nothing starts a model.

See ``webchat/webchat.spec.md`` (Attachments).
"""
import dataclasses
import sys

import pytest

from jarvis import daemon
from jarvis.bridge import modes, runtime
from jarvis.memory.conversation import DialogueMemory
from jarvis.reply.attachments import RequestAttachment
from jarvis.tools.types import ToolExecutionResult, ToolImage
from jarvis.webchat.backend import DaemonBackend
from jarvis.webchat.uploads import UploadRefused


@dataclasses.dataclass(frozen=True)
class Cfg:
    llm_provider: str = "ollama"
    llm_chat_model: str = "gemma4:12b"
    screen_awareness_enabled: bool = True
    windows_tools_enabled: bool = True


class FakeLLM:
    def __init__(self, sees=True):
        self.sees = sees
        self.asked = []

    def supports_images(self, model):
        self.asked.append(model)
        return self.sees


@pytest.fixture
def world(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(daemon, "_global_cfg", Cfg())
    monkeypatch.setattr(daemon, "_global_dialogue_memory", DialogueMemory())
    monkeypatch.setattr("jarvis.llm.get_llm_backend", lambda cfg: llm)
    monkeypatch.setattr(modes, "active_mode", lambda: "local")
    monkeypatch.setattr(sys, "platform", "win32")
    return llm


def test_attachments_go_to_the_text_path_with_the_request(world, monkeypatch):
    seen = {}
    monkeypatch.setattr(daemon, "submit_text_query", lambda text, **kw: seen.update(text=text, **kw))
    attached = (RequestAttachment("text", 3, text="body"),)
    DaemonBackend().submit("hello", on_start=print, on_complete=print, on_busy=print, attachments=attached)
    assert seen["attachments"] == attached and seen["origin"] == "chat"


class TestWhatImagesCanGo:
    def test_a_local_vision_model_takes_images(self, world):
        assert DaemonBackend().attachment_support()["images"] == {"ok": True, "reason": None}
        assert world.asked == ["gemma4:12b"]

    def test_a_local_model_without_vision_says_so(self, world):
        world.sees = False
        assert DaemonBackend().attachment_support()["images"] == {"ok": False, "reason": "no_vision"}

    def test_a_backend_that_cannot_tell_counts_as_no_vision(self, world, monkeypatch):
        def broken(cfg):
            raise RuntimeError("down")

        monkeypatch.setattr("jarvis.llm.get_llm_backend", broken)
        assert DaemonBackend().attachment_support()["images"]["reason"] == "no_vision"

    def test_a_running_cloud_bridge_takes_images(self, world, monkeypatch):
        monkeypatch.setattr(modes, "active_mode", lambda: "claude")
        monkeypatch.setattr(runtime, "get_service", lambda: type("S", (), {"accepts_images": True})())
        assert DaemonBackend().attachment_support()["images"] == {"ok": True, "reason": None}
        assert world.asked == []  # the local model is not consulted in a cloud mode

    def test_a_cloud_bridge_that_takes_no_images_says_so(self, world, monkeypatch):
        monkeypatch.setattr(modes, "active_mode", lambda: "codex")
        monkeypatch.setattr(runtime, "get_service", lambda: type("S", (), {})())
        assert DaemonBackend().attachment_support()["images"] == {"ok": False, "reason": "cloud_no_images"}

    def test_a_cloud_bridge_not_yet_running_is_not_ready(self, world, monkeypatch):
        monkeypatch.setattr(modes, "active_mode", lambda: "claude")
        monkeypatch.setattr(runtime, "get_service", lambda: None)
        assert DaemonBackend().attachment_support()["images"] == {"ok": False, "reason": "cloud_not_ready"}


class TestScreenshotSupport:
    def test_available_on_windows_with_screen_awareness(self, world):
        assert DaemonBackend().attachment_support()["screenshot"] == {"ok": True, "reason": None}

    def test_screen_awareness_off_is_the_reason(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", Cfg(screen_awareness_enabled=False))
        assert DaemonBackend().attachment_support()["screenshot"] == {"ok": False, "reason": "screen_awareness_off"}

    def test_other_platforms_and_missing_windows_tools_have_no_screenshot(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", Cfg(windows_tools_enabled=False))
        assert DaemonBackend().attachment_support()["screenshot"]["reason"] == "screenshot_unavailable"
        monkeypatch.setattr(daemon, "_global_cfg", Cfg())
        monkeypatch.setattr(sys, "platform", "linux")
        assert DaemonBackend().attachment_support()["screenshot"]["reason"] == "screenshot_unavailable"

    def test_before_the_daemon_has_booted_nothing_is_available(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", None)
        assert DaemonBackend().attachment_support()["screenshot"]["ok"] is False


class FakeScreenshotTool:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, args, context):
        self.calls.append((args, context.cfg))
        return self.result


class TestScreenshotButton:
    def test_the_screenshot_tools_own_path_provides_the_image_and_its_screen_text(self, world):
        image = ToolImage("image/jpeg", "QUJDREVGR0hJSktM")  # 12 bytes once decoded
        tool = FakeScreenshotTool(ToolExecutionResult(True, "header\nthe fenced OCR text", images=(image,)))
        attached = DaemonBackend(screenshot_tool=tool).capture_screenshot()
        assert attached == RequestAttachment("screenshot", 12, image=image, text="header\nthe fenced OCR text")
        assert tool.calls[0][0] == {}  # the default target: the monitor the user is looking at

    def test_screen_awareness_off_stops_before_anything_is_captured(self, world, monkeypatch):
        monkeypatch.setattr(daemon, "_global_cfg", Cfg(screen_awareness_enabled=False))
        tool = FakeScreenshotTool(ToolExecutionResult(True, "x", images=(ToolImage("image/jpeg", "QQ=="),)))
        with pytest.raises(UploadRefused) as refused:
            DaemonBackend(screenshot_tool=tool).capture_screenshot()
        assert refused.value.code == "screen_awareness_off" and tool.calls == []

    def test_a_failed_capture_is_reported_without_its_detail(self, world):
        tool = FakeScreenshotTool(ToolExecutionResult(False, "locked", error_message="locked"))
        with pytest.raises(UploadRefused) as refused:
            DaemonBackend(screenshot_tool=tool).capture_screenshot()
        assert refused.value.code == "screenshot_unavailable" and refused.value.status == 503

    def test_a_capture_with_no_image_is_a_failure(self, world):
        tool = FakeScreenshotTool(ToolExecutionResult(True, "text only"))
        with pytest.raises(UploadRefused):
            DaemonBackend(screenshot_tool=tool).capture_screenshot()
