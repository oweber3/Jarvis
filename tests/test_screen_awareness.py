"""Screen awareness: the screenshot tool on Windows captures what the user asked about, reads it locally,
and hands the model the text (fenced) and one scaled image. See ``tools/builtin/screenshot.spec.md``."""

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from jarvis.platform.windows.displays import Monitor
from jarvis.tools.builtin import screenshot as screenshot_module
from jarvis.tools.builtin.screenshot import MAX_IMAGE_EDGE, MAX_OCR_CHARS, ScreenshotTool
from jarvis.tools.base import ToolContext

pytestmark = pytest.mark.unit

MAIN = Monitor(device=r"\\.\DISPLAY1", bounds=(0, 0, 2560, 1440), work_area=(0, 0, 2560, 1400), primary=True)
SIDE = Monitor(device=r"\\.\DISPLAY2", bounds=(-1080, -162, 0, 1758), work_area=(-1080, -162, 0, 1718),
               primary=False)
CHROME = {"hwnd": 101, "process": "chrome", "application": "Google Chrome", "monitor": SIDE.device,
          "state": "normal"}
SECRET_TEXT = "Invoice 7781 for Alice Example"


class FakeScreen:
    def __init__(self, lines=("Hello", "World"), monitors=(MAIN, SIDE), window=(-1000, 0, -200, 600),
                 fail=None):
        self.lines, self.monitors, self.window, self.fail = lines, list(monitors), window, fail
        self.captured = []

    def list_monitors(self):
        return list(self.monitors)

    def capture(self, bounds):
        if self.fail:
            raise self.fail
        self.captured.append(tuple(bounds))
        left, top, right, bottom = bounds
        return Image.new("RGB", (right - left, bottom - top), "white")

    def window_bounds(self, hwnd):
        return self.window

    def read_text(self, image):
        return None if self.lines is None else list(self.lines)


def cfg(**kw):
    base = dict(screen_awareness_enabled=True, windows_tools_enabled=True, windows_monitor_aliases={"side": SIDE.device})
    base.update(kw)
    return SimpleNamespace(**base)


def run(args=None, screen=None, foreground=CHROME, config=None):
    screen = screen or FakeScreen()
    tool = ScreenshotTool(screen=screen, foreground=lambda: foreground, platform="win32")
    context = ToolContext(db=None, cfg=config or cfg(), system_prompt="", original_prompt="", redacted_text="",
                          max_retries=0, user_print=lambda _m: None)
    return tool.run(args or {}, context), screen


def decoded(result):
    (image,) = result.images
    assert image.mime_type == "image/jpeg"
    return Image.open(io.BytesIO(base64.b64decode(image.data)))


def test_by_default_it_captures_the_monitor_of_the_window_the_user_is_looking_at():
    result, screen = run()
    assert result.success
    assert screen.captured == [SIDE.bounds]
    assert "Google Chrome" in result.reply_text and SIDE.device in result.reply_text


def test_the_text_is_fenced_as_untrusted_screen_content_in_reading_order():
    result, _ = run()
    text = result.reply_text
    start, end = text.index("<<<BEGIN UNTRUSTED SCREEN TEXT>>>"), text.index("<<<END UNTRUSTED SCREEN TEXT>>>")
    assert start < text.index("Hello") < text.index("World") < end


def test_the_model_receives_one_image_scaled_to_the_edge_limit_and_never_enlarged():
    big, _ = run()
    assert max(decoded(big).size) == MAX_IMAGE_EDGE
    small, _ = run({"target": "window"}, screen=FakeScreen(window=(0, 0, 400, 300)))
    assert decoded(small).size == (400, 300)


def test_with_no_application_window_in_front_the_primary_monitor_is_captured():
    result, screen = run(foreground=None)
    assert result.success and screen.captured == [MAIN.bounds]


def test_a_named_monitor_is_resolved_like_window_placement():
    by_number, screen = run({"monitor": "1"})
    assert screen.captured == [MAIN.bounds]
    by_alias, screen = run({"monitor": "side"})
    assert screen.captured == [SIDE.bounds]


def test_an_unknown_monitor_fails_with_the_connected_monitors_and_captures_nothing():
    result, screen = run({"monitor": "kitchen"})
    assert not result.success and screen.captured == []
    assert MAIN.device in result.error_message and SIDE.device in result.error_message


def test_all_captures_every_monitor_in_one_image():
    result, screen = run({"target": "all"})
    assert result.success and screen.captured == [(-1080, -162, 2560, 1758)]


def test_window_captures_the_window_the_user_is_looking_at():
    result, screen = run({"target": "window"})
    assert result.success and screen.captured == [(-1000, 0, -200, 600)]


def test_window_without_an_application_window_in_front_fails_honestly():
    result, screen = run({"target": "window"}, foreground=None)
    assert not result.success and screen.captured == []


def test_a_minimised_window_cannot_be_captured():
    result, screen = run({"target": "window"}, screen=FakeScreen(window=None))
    assert not result.success and screen.captured == []


def test_without_ocr_the_image_is_still_returned_and_the_text_is_said_to_be_unreadable():
    result, _ = run(screen=FakeScreen(lines=None))
    assert result.success and len(result.images) == 1
    assert "<<<BEGIN UNTRUSTED SCREEN TEXT>>>" not in result.reply_text


def test_long_screen_text_is_capped_and_the_cut_is_marked():
    result, _ = run(screen=FakeScreen(lines=["x" * 200] * 200))
    fenced = result.reply_text.split("<<<BEGIN UNTRUSTED SCREEN TEXT>>>")[1].split("<<<END")[0]
    assert len(fenced) < MAX_OCR_CHARS + 200 and "…" in fenced


def test_a_capture_failure_is_reported_without_an_image():
    result, _ = run(screen=FakeScreen(fail=OSError("protected desktop")))
    assert not result.success and not result.images
    assert "could not" in result.error_message.casefold()


def test_screen_awareness_off_refuses_and_captures_nothing():
    result, screen = run(config=cfg(screen_awareness_enabled=False))
    assert not result.success and screen.captured == [] and not result.images
    assert "settings" in result.error_message.casefold()


def test_screen_text_never_reaches_the_debug_log(monkeypatch):
    logged = []
    monkeypatch.setattr(screenshot_module, "debug_log", lambda message, *_a, **_k: logged.append(message))
    run(screen=FakeScreen(lines=[SECRET_TEXT]))
    assert logged and not any("7781" in line or "Alice" in line for line in logged)


def test_looking_at_the_screen_needs_no_confirmation():
    from jarvis.tools.confirmation import SafetyTier
    assert ScreenshotTool().classify_safety({}, cfg()).tier == SafetyTier.SAFE


@pytest.mark.parametrize("enabled, expected", [(True, True), (False, False)])
def test_the_setting_decides_whether_the_tool_is_offered(enabled, expected):
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_screen_tool
    saved = BUILTIN_TOOLS.get("screenshot")
    try:
        configure_screen_tool(cfg(screen_awareness_enabled=enabled), platform="win32")
        assert ("screenshot" in BUILTIN_TOOLS) is expected
    finally:
        BUILTIN_TOOLS.pop("screenshot", None)
        if saved is not None:
            BUILTIN_TOOLS["screenshot"] = saved


def test_on_windows_the_tool_also_needs_the_windows_tools():
    from jarvis.tools.registry import BUILTIN_TOOLS, configure_screen_tool
    saved = BUILTIN_TOOLS.get("screenshot")
    try:
        configure_screen_tool(cfg(windows_tools_enabled=False), platform="win32")
        assert "screenshot" not in BUILTIN_TOOLS
    finally:
        BUILTIN_TOOLS.pop("screenshot", None)
        if saved is not None:
            BUILTIN_TOOLS["screenshot"] = saved
