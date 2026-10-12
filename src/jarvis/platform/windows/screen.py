"""Screen capture and offline OCR on Windows.

Rectangles are ``(left, top, right, bottom)`` in physical pixels in the virtual screen, as in
``displays``. Nothing here keeps, writes or logs what is on the screen. See
``tools/builtin/screenshot.spec.md``.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import math
from dataclasses import dataclass
from typing import Optional

from ...debug import debug_log
from . import displays
from .displays import Monitor, Rectangle

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_JPEG_QUALITY = 85
# Neighbouring OCR tiles share at least this many pixels, so a word cut by one tile's edge is seen
# whole in the tile that keeps it (each word is kept once, by the tile owning its centre).
_OCR_TILE_OVERLAP = 128


def list_monitors() -> list[Monitor]:
    return displays.list_monitors()


def capture(bounds: Rectangle):
    """The pixels inside ``bounds`` as a PIL image. Raises ``OSError`` when Windows refuses the capture
    (a protected desktop such as the lock screen or a UAC prompt)."""
    from PIL import ImageGrab
    with displays.per_monitor_dpi():
        return ImageGrab.grab(bbox=tuple(bounds), all_screens=True)


def window_bounds(hwnd: int) -> Optional[Rectangle]:
    """The window's visible frame on screen, or ``None`` when it is minimised."""
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    if user.IsIconic(wintypes.HWND(hwnd)):
        return None
    rect = wintypes.RECT()
    with displays.per_monitor_dpi():
        result = ctypes.WinDLL('dwmapi').DwmGetWindowAttribute(
            wintypes.HWND(hwnd), _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
        if result != 0 and not user.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _ocr_engine():
    from winrt.windows.media.ocr import OcrEngine
    return OcrEngine, OcrEngine.try_create_from_user_profile_languages()


@dataclass(frozen=True)
class OcrWord:
    """One recognised word and its box in the image it was read from."""
    text: str
    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True)
class OcrLine:
    text: str
    words: tuple[OcrWord, ...]


async def _recognise(engine, image) -> list[OcrLine]:
    """The engine's lines for ``image``, in its reading order, with word boxes."""
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    writer = DataWriter()
    writer.write_bytes(image.tobytes('raw', 'BGRA'))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                    image.width, image.height)
    result = await engine.recognize_async(bitmap)
    lines = []
    for line in result.lines:
        words = []
        for word in line.words:
            box = word.bounding_rect
            words.append(OcrWord(word.text, box.x, box.y, box.x + box.width, box.y + box.height))
        lines.append(OcrLine(line.text, tuple(words)))
    return lines


def _tiles(length: int, limit: int) -> tuple[int, list[int]]:
    """``(size, starts)``: the fewest equal tiles of at most ``limit`` pixels covering one side, with
    neighbours sharing at least ``_OCR_TILE_OVERLAP`` pixels."""
    if length <= limit:
        return length, [0]
    count = math.ceil((length - _OCR_TILE_OVERLAP) / max(1, limit - _OCR_TILE_OVERLAP))
    size = math.ceil((length + (count - 1) * _OCR_TILE_OVERLAP) / count)
    return size, [round(i * (length - size) / (count - 1)) for i in range(count)]


def _owned_ranges(starts: list[int], size: int) -> list[tuple[float, float]]:
    """Each tile owns the part of the side up to the middle of its overlaps, so a word seen in two
    tiles is kept once: by the tile whose owned range holds the word's centre."""
    cuts = [(starts[i] + size + starts[i + 1]) / 2 for i in range(len(starts) - 1)]
    edges = [float('-inf'), *cuts, float('inf')]
    return [(edges[i], edges[i + 1]) for i in range(len(starts))]


def read_text(image) -> Optional[list[str]]:
    """Lines of text in ``image`` in reading order, read by Windows' built-in OCR in the user's
    installed OCR languages, at full resolution: an image larger than the engine accepts is read in
    equal tiles, row by row, each tile's lines in the engine's order. ``None`` when OCR is
    unavailable (no OCR language, or no runtime)."""
    try:
        engine_type, engine = _ocr_engine()
    except (ImportError, OSError) as exc:
        debug_log(f'screen OCR unavailable ({type(exc).__name__})', 'screen')
        return None
    if engine is None:
        debug_log('screen OCR unavailable (no OCR language installed)', 'screen')
        return None
    rgba = image.convert('RGBA')
    limit = int(engine_type.max_image_dimension)
    width, height = rgba.size
    try:
        if max(width, height) <= limit:
            return [line.text for line in asyncio.run(_recognise(engine, rgba))]
        (tile_width, xs), (tile_height, ys) = _tiles(width, limit), _tiles(height, limit)
        x_owned, y_owned = _owned_ranges(xs, tile_width), _owned_ranges(ys, tile_height)
        debug_log(f'screen OCR reading {width}x{height} in {len(xs) * len(ys)} tiles', 'screen')
        lines = []
        for row, y in enumerate(ys):
            for column, x in enumerate(xs):
                tile = rgba.crop((x, y, x + tile_width, y + tile_height))
                for line in asyncio.run(_recognise(engine, tile)):
                    kept = [w.text for w in line.words
                            if x_owned[column][0] <= x + (w.left + w.right) / 2 < x_owned[column][1]
                            and y_owned[row][0] <= y + (w.top + w.bottom) / 2 < y_owned[row][1]]
                    if len(kept) == len(line.words):
                        lines.append(line.text)
                    elif kept:
                        lines.append(' '.join(kept))
        return lines
    except OSError as exc:
        debug_log(f'screen OCR failed ({type(exc).__name__})', 'screen')
        return None


def encode_for_model(image, max_edge: int) -> tuple[str, str]:
    """``(mime_type, base64 data)`` of ``image`` as JPEG, scaled down (never up) so its longer side
    is at most ``max_edge``."""
    from PIL import Image
    copy = image.convert('RGB')
    if max(copy.size) > max_edge:
        copy.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    copy.save(buffer, format='JPEG', quality=_JPEG_QUALITY)
    return 'image/jpeg', base64.b64encode(buffer.getvalue()).decode('ascii')
