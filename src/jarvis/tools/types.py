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
