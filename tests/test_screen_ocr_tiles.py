"""Screen OCR reads the full-resolution capture (``tools/builtin/screenshot.spec.md``, Result). An image larger than
the OCR engine accepts (its ``max_image_dimension``, read from the engine) is read in equal tiles, row by row,
each tile's lines in the engine's own order and each word once. The engine is a fake that reads a synthetic page
with a small limit; nothing captures the real screen."""

import numpy as np
import pytest
from PIL import Image

from jarvis.platform.windows import screen

pytestmark = pytest.mark.unit


def _page_image(width, height):
    """Every pixel spells its own coordinates, so the fake engine knows where a tile came from and whether it
    was scaled."""
    xs, ys = np.meshgrid(np.arange(width), np.arange(height))
    pixels = np.stack([xs & 255, xs >> 8, ys & 255, (ys >> 8) + 1], axis=-1).astype(np.uint8)
    return Image.fromarray(pixels, "RGBA")


def _origin(image, x, y):
    r, g, b, a = image.getpixel((x, y))
    return r + (g << 8), b + ((a - 1) << 8)


def fake_engine(limit):
    class FakeOcr:
        """Reads ``page`` (lines of ``(text, left, top, right, bottom)`` words, in the engine's order) as the real
        engine would: whole words inside the tile, and the visible part of a word the tile edge cuts."""

        max_image_dimension = limit

        def __init__(self, page):
            self.page, self.tiles = page, []

        async def recognise(self, _engine, tile):
            width, height = tile.size
            assert max(width, height) <= self.max_image_dimension  # what the real engine enforces
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

    return FakeOcr


def _read(monkeypatch, engine_type, page, size):
    fake = engine_type(page)
    monkeypatch.setattr(screen, "_ocr_engine", lambda: (engine_type, object()))
    monkeypatch.setattr(screen, "_recognise", fake.recognise)
    return screen.read_text(_page_image(*size)), fake


def test_an_image_the_engine_accepts_is_read_once_in_the_engines_order(monkeypatch):
    engine = fake_engine(1000)
    page = [[("second", 10, 300, 100, 330)], [("first", 10, 100, 100, 130)]]
    lines, fake = _read(monkeypatch, engine, page, (engine.max_image_dimension, 600))
    assert fake.tiles == [(0, 0, engine.max_image_dimension, 600)]
    assert lines == ["second", "first"]


def test_a_wider_image_is_read_in_equal_unscaled_tiles_each_word_once(monkeypatch):
    engine = fake_engine(1000)
    width = engine.max_image_dimension * 2 + 300
    left_tile = [("left", 20, 100, 120, 130)]
    right_tile = [("right", width - 150, 100, width - 20, 130)]
    lines, fake = _read(monkeypatch, engine, [left_tile, right_tile], (width, 500))
    sizes = {(w, h) for _l, _t, w, h in fake.tiles}
    assert len(fake.tiles) == 3 and len(sizes) == 1, "tiles should be equal"
    assert fake.tiles[0][0] == 0 and fake.tiles[-1][0] + fake.tiles[-1][2] == width
    assert lines == ["left", "right"]

    # A word in the overlap of two tiles is seen whole by both and kept once.
    first, second = fake.tiles[0], fake.tiles[1]
    overlap_left, overlap_right = second[0], first[0] + first[2]
    middle = (overlap_left + overlap_right) // 2
    straddling = [("edge", middle - 30, 300, middle + 30, 330)]
    lines, _ = _read(monkeypatch, engine, [straddling], (width, 500))
    assert lines == ["edge"]


def test_each_tile_keeps_the_engines_line_order(monkeypatch):
    """Tiles are read row by row, left to right, and their lines are not re-ordered across tiles."""
    engine = fake_engine(1000)
    width = engine.max_image_dimension * 2 - 200
    page = [[("lower-left", 20, 400, 200, 430)], [("upper-left", 20, 100, 200, 130)],
            [("right", width - 200, 50, width - 20, 80)]]
    lines, fake = _read(monkeypatch, engine, page, (width, 500))
    assert len(fake.tiles) == 2
    assert lines == ["lower-left", "upper-left", "right"]


def test_a_tall_image_is_tiled_vertically_too(monkeypatch):
    engine = fake_engine(1000)
    height = engine.max_image_dimension + 500
    page = [[("top", 50, 40, 150, 70)], [("bottom", 50, height - 60, 200, height - 30)]]
    lines, fake = _read(monkeypatch, engine, page, (800, height))
    assert len({top for _l, top, _w, _h in fake.tiles}) == 2
    assert lines == ["top", "bottom"]
