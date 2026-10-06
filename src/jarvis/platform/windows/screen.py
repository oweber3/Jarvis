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
from typing import Optional

from ...debug import debug_log
from . import displays
from .displays import Monitor, Rectangle

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_JPEG_QUALITY = 85


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


async def _recognise(engine, image) -> list[str]:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter
    writer = DataWriter()
    writer.write_bytes(image.tobytes('raw', 'BGRA'))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                    image.width, image.height)
    result = await engine.recognize_async(bitmap)
    return [line.text for line in result.lines]


def read_text(image) -> Optional[list[str]]:
    """Lines of text in ``image`` in reading order, read by Windows' built-in OCR in the user's
    installed OCR languages. ``None`` when OCR is unavailable (no OCR language, or no runtime)."""
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
    if max(rgba.size) > limit:
        rgba.thumbnail((limit, limit))
    try:
        return asyncio.run(_recognise(engine, rgba))
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
