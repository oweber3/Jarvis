"""What the owner attaches in the web chat: told apart by content, prepared for a model, held in memory.

A file is never trusted by its name or declared type, and its path never reaches this package: the page
sends bytes. An upload exists only in the ``UploadStore`` until the request that carries it takes it (or it
expires); nothing is written to disk or logged. See ``webchat.spec.md`` (Attachments).
"""
from __future__ import annotations

import base64
import io
import secrets
import threading
import time
from typing import Callable, Dict, List, Sequence, Tuple

from ..debug import debug_log
from ..reply.attachments import (
    KIND_IMAGE,
    KIND_TEXT,
    MAX_ATTACHMENTS,
    MAX_FILE_CHARS,
    MAX_IMAGES,
    RequestAttachment,
    fence_file_text,
)
from ..tools.types import ToolImage

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_PIXELS = 50_000_000
PDF_MAX_PAGES = 100
PDF_BUDGET_SEC = 6.0
JPEG_QUALITY = 85
HOLD_SEC = 15 * 60
MAX_HELD = 8

_IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
_MAX_CONTROL_SHARE = 0.02
_BINARY_PROBE_BYTES = 8192

_STATUS = {"empty": 400, "too_large": 413, "unsupported": 415, "unreadable": 422, "no_text": 422,
           "encrypted": 422, "unknown_attachment": 404, "too_many": 409, "no_vision": 409, "cloud_no_images": 409,
           "screen_awareness_off": 409, "screenshot_unavailable": 503, "cloud_not_ready": 503}


class UploadRefused(Exception):
    """An upload or attachment that cannot be accepted, with a stable ``code`` for the page to explain."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status = _STATUS.get(code, 400)


# -- telling files apart ------------------------------------------------------------------------------


def _is_image(data: bytes) -> bool:
    return (data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _is_pdf(data: bytes) -> bool:
    return b"%PDF-" in data[:1024]


def classify(data: bytes) -> RequestAttachment:
    """Decide what ``data`` is from its content and prepare it, or raise ``UploadRefused``."""
    if not data:
        raise UploadRefused("empty")
    if _is_image(data):
        return _image(data)
    if _is_pdf(data):
        return _pdf(data)
    return _text(data)


def _image(data: bytes) -> RequestAttachment:
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadRefused("too_large")
    from PIL import Image, UnidentifiedImageError
    from ..tools.builtin.screenshot import MAX_IMAGE_EDGE
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if opened.format not in _IMAGE_FORMATS:
                raise UploadRefused("unsupported")
            if opened.width * opened.height > MAX_PIXELS:
                raise UploadRefused("too_large")
            opened.load()
            frame = opened.convert("RGBA") if (opened.mode in ("RGBA", "LA", "PA") or "transparency" in opened.info
                                                or opened.mode == "P") else opened.convert("RGB")
        if frame.mode == "RGBA":  # transparency is flattened onto white; the JPEG has no alpha
            flat = Image.new("RGB", frame.size, "white")
            flat.paste(frame, mask=frame.getchannel("A"))
            frame = flat
        if max(frame.size) > MAX_IMAGE_EDGE:
            frame.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=JPEG_QUALITY)  # re-encoding drops metadata such as location
    except UploadRefused:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        debug_log(f"webchat: image upload unreadable ({type(exc).__name__})", "webchat")
        raise UploadRefused("unreadable") from exc
    sent = base64.b64encode(buffer.getvalue()).decode("ascii")
    debug_log(f"webchat: image upload accepted ({len(data)} bytes)", "webchat")
    return RequestAttachment(KIND_IMAGE, len(data), image=ToolImage("image/jpeg", sent))


def _pdf(data: bytes) -> RequestAttachment:
    if len(data) > MAX_PDF_BYTES:
        raise UploadRefused("too_large")
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UploadRefused("encrypted")
        pages = len(reader.pages)
        deadline = time.monotonic() + PDF_BUDGET_SEC
        chunks: List[str] = []
        total = 0
        for index in range(min(pages, PDF_MAX_PAGES)):
            chunks.append(reader.pages[index].extract_text() or "")
            total += len(chunks[-1])
            if total > MAX_FILE_CHARS or time.monotonic() > deadline:
                break
    except UploadRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many kinds for damaged files
        debug_log(f"webchat: PDF upload unreadable ({type(exc).__name__})", "webchat")
        raise UploadRefused("unreadable") from exc
    text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    if not text:
        raise UploadRefused("no_text")
    debug_log(f"webchat: PDF upload accepted ({len(data)} bytes, {pages} pages)", "webchat")
    return RequestAttachment(KIND_TEXT, len(data), text=fence_file_text(f"(PDF, {pages} pages)\n{text}"))


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\0" in data[:_BINARY_PROBE_BYTES]:
        raise UploadRefused("unsupported")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")  # Windows' own code page; a byte it leaves undefined means binary


def _text(data: bytes) -> RequestAttachment:
    if len(data) > MAX_TEXT_BYTES:
        raise UploadRefused("too_large")
    try:
        text = _decode(data)
    except UnicodeDecodeError as exc:
        raise UploadRefused("unsupported") from exc
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\t\n\r\f")
    if not text.strip() or controls / len(text) > _MAX_CONTROL_SHARE:
        raise UploadRefused("unsupported" if text.strip() else "empty")
    debug_log(f"webchat: text upload accepted ({len(data)} bytes)", "webchat")
    return RequestAttachment(KIND_TEXT, len(data), text=fence_file_text(text))


# -- holding uploads until a request takes them --------------------------------------------------------


class UploadStore:
    """Uploads waiting for the request that will carry them. In memory only; each is handed over once."""

    def __init__(self, *, ttl_sec: float = HOLD_SEC, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_sec
        self._clock = clock
        self._lock = threading.Lock()
        self._held: Dict[str, Tuple[RequestAttachment, float]] = {}

    def __len__(self) -> int:
        with self._lock:
            self._expire()
            return len(self._held)

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, (_a, at) in self._held.items() if now - at > self._ttl]:
            del self._held[key]

    def add(self, attachment: RequestAttachment) -> str:
        with self._lock:
            self._expire()
            if len(self._held) >= MAX_HELD:
                raise UploadRefused("too_many")
            key = secrets.token_hex(16)
            self._held[key] = (attachment, self._clock())
            return key

    def _lookup(self, ids: Sequence[str]) -> List[RequestAttachment]:
        self._expire()
        if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
            raise UploadRefused("too_many")
        found = []
        for key in ids:
            entry = self._held.get(key) if isinstance(key, str) else None
            if entry is None:
                raise UploadRefused("unknown_attachment")
            found.append(entry[0])
        if sum(1 for a in found if a.image is not None) > MAX_IMAGES:
            raise UploadRefused("too_many")
        return found

    def peek(self, ids: Sequence[str]) -> List[RequestAttachment]:
        """The attachments for ``ids`` without handing them over, or ``UploadRefused``."""
        with self._lock:
            return self._lookup(ids)

    def take(self, ids: Sequence[str]) -> List[RequestAttachment]:
        """Hand over the attachments for ``ids``, all or none."""
        with self._lock:
            found = self._lookup(ids)
            for key in ids:
                del self._held[key]
            return found

    def discard(self, key: str) -> None:
        with self._lock:
            self._held.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._held.clear()
