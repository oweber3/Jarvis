"""The Windows screen layer on the real OS: capture size, offline OCR and image encoding.

Read-only: the capture is checked for its size only and never written to disk or printed."""

import base64
import io
import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

pytestmark = [pytest.mark.unit, pytest.mark.skipif(sys.platform != "win32", reason="Windows only")]


def _text_image(text):
    image = Image.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.truetype("arial.ttf", 64)
    except OSError:
        pytest.skip("no TrueType font to render the fixture")
    ImageDraw.Draw(image).text((20, 40), text, fill="black", font=font)
    return image


def test_ocr_reads_text_from_an_image_offline():
    from jarvis.platform.windows import screen
    lines = screen.read_text(_text_image("Jarvis sees 4217"))
    if lines is None:
        pytest.skip("no Windows OCR language installed")
    joined = " ".join(lines)
    assert "Jarvis" in joined and "4217" in joined


def test_capturing_a_monitor_returns_an_image_of_its_physical_size():
    from jarvis.platform.windows import displays, screen
    monitor = displays.list_monitors()[0]
    image = screen.capture(monitor.bounds)
    left, top, right, bottom = monitor.bounds
    assert image.size == (right - left, bottom - top)


@pytest.mark.parametrize("size, edge, expected", [((3000, 1500), 1000, (1000, 500)), ((400, 300), 1000, (400, 300))])
def test_images_for_the_model_are_scaled_down_only_and_jpeg_encoded(size, edge, expected):
    from jarvis.platform.windows import screen
    mime, data = screen.encode_for_model(Image.new("RGBA", size, "white"), edge)
    assert mime == "image/jpeg"
    assert Image.open(io.BytesIO(base64.b64decode(data))).size == expected
