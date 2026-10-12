"""Common types and result classes for tools."""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ToolImage:
    """An image a tool hands the model beside its text: MIME type and base64 data. Never stored."""
    mime_type: str
    data: str


@dataclass
class ToolExecutionResult:
    """Result object for tool execution."""
    success: bool
    reply_text: Optional[str]
    error_message: Optional[str] = None
    # Delivered with this result only, to models that can see (screenshot.spec.md, Delivery to the model).
    images: Tuple[ToolImage, ...] = ()


@dataclass(frozen=True)
class StepNote:
    """How a tool words one finished call in the web chat's tool steps (``Tool.step_note``).

    ``label`` is a short action phrase and ``summary`` an optional short outcome. Both are shown to the
    owner, so they hold nothing the tool's spec keeps out of results: no paths, addresses, window titles
    or content."""
    label: str
    summary: Optional[str] = None
