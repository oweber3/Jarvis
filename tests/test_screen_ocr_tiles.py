"""Screen OCR reads the full-resolution capture (``tools/builtin/screenshot.spec.md``, Result): an image larger
than Windows' OCR engine accepts is read in overlapping tiles and the lines are joined in reading order, each
once. The OCR engine is a fake that reads a synthetic page; nothing captures the real screen."""

import numpy as np
import pytest
from PIL import Image

from jarvis.platform.windows import screen

pytestmark = pytest.mark.unit

LIMIT = 2600  # Windows' OcrEngine.MaxImageDimension


def _page_image(width, height):
    """Every pixel spells its own coordinates, so the fake engine knows where a tile came from and whether it
    was scaled."""
    xs, ys = np.meshgrid(np.arange(width), np.arange(height))
    pixels = np.stack([xs & 255, xs >> 8, ys & 255, (ys >> 8) + 1], axis=-1).astype(np.uint8)
    return Image.fromarray(pixels, "RGBA")


def _origin(image, x, y):
    r, g, b, a = image.getpixel((x, y))
    return r + (g << 8), b + ((a - 1) << 8)


class FakeOcr:
    """Reads ``page`` (lines of ``(text, left, top, right, bottom)`` words) the way the real engine would: whole
    words inside the tile, and the visible part of a word the tile edge cuts."""

    max_image_dimension = LIMIT

    def __init__(self, page):
        self.page, self.tiles = page, []

    async def recognise(self, _engine, tile):
        width, height = tile.size
        assert max(width, height) <= LIMIT
        left, top = _origin(tile, 0, 0)
        assert _origin(tile, width - 1, height - 1) == (left + width - 1, top + height - 1), "tile was scaled"
        self.tiles.append((left, top, width, height))
        lines = []
        for words in self.page:
            seen = []
            for text, l, t, r, b in words:
                cl, cr = max(l, left), min(r, left + width)
                if cr <= cl or t < top or b > top + height:
                    continue
                shown = text[:max(1, round(len(text) * (cr - cl) / (r - l)))]
                seen.append(screen.OcrWord(shown, cl - left, t - top, cr - left, b - top))
            if seen:
                lines.append(screen.OcrLine(" ".join(w.text for w in seen), tuple(seen)))
        return lines


def _read(monkeypatch, page, size):
    fake = FakeOcr(page)
    monkeypatch.setattr(screen, "_ocr_engine", lambda: (FakeOcr, object()))
    monkeypatch.setattr(screen, "_recognise", fake.recognise)
    return screen.read_text(_page_image(*size)), fake


def test_a_4k_capture_is_read_at_full_resolution_in_reading_order_without_duplicates(monkeypatch):
    long_line = [(f"w{i}", 20 + 150 * i, 100, 140 + 150 * i, 130) for i in range(25)]  # crosses every tile edge
    middle = [("middle", 1900, 400, 2000, 430)]  # inside the overlap: seen whole by both tiles
    status = [("status", 10, 2100, 80, 2130)]
    tiny = [("tiny", 3700, 2100, 3760, 2130)]  # small text far right, which a downscale would blur
    lines, fake = _read(monkeypatch, [long_line, middle, status, tiny], (3840, 2160))

    assert len(fake.tiles) > 1
    assert lines == [" ".join(f"w{i}" for i in range(25)), "middle", "status", "tiny"]


def test_a_tall_capture_is_tiled_vertically_too(monkeypatch):
    page = [[("top", 50, 40, 150, 70)], [("cut", 50, 2560, 150, 2590)], [("bottom", 50, 3800, 200, 3830)]]
    lines, fake = _read(monkeypatch, page, (1200, 3900))
    assert len({top for _l, top, _w, _h in fake.tiles}) > 1
    assert lines == ["top", "cut", "bottom"]


def test_a_capture_the_engine_accepts_is_read_once_in_the_engines_order(monkeypatch):
    page = [[("second", 10, 300, 100, 330)], [("first", 10, 100, 100, 130)]]
    lines, fake = _read(monkeypatch, page, (1920, 1080))
    assert fake.tiles == [(0, 0, 1920, 1080)]
    assert lines == ["second", "first"]
