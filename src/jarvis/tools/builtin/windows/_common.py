"""Helpers shared by the Windows control tool adapters."""

from __future__ import annotations

import re
from typing import Any, Optional

from ...base import ToolContext
from ...types import ToolExecutionResult

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def disabled_result(context: ToolContext) -> Optional[ToolExecutionResult]:
    """A refusal when Windows tools are switched off in config, else ``None``."""
    if getattr(context.cfg, "windows_tools_enabled", True):
        return None
    message = (
        "Windows control tools are disabled in your configuration. "
        "To enable them, set 'windows_tools_enabled': true in your config.json file."
    )
    return ToolExecutionResult(success=False, reply_text=message, error_message=message)


def parse_number(value: Any) -> Optional[float]:
    """Read a number from loosely typed model output such as 30, "30" or "30%"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER_RE.search(value)
        if match:
            return float(match.group())
    return None


def failure(message: str) -> ToolExecutionResult:
    return ToolExecutionResult(success=False, reply_text=message, error_message=message)
