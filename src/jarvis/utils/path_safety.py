"""Paths that reach another computer or a device when touched, recognised from their text alone.

On Windows, resolving, opening or even checking a UNC path (``\\\\host\\share\\...`` or
``\\\\?\\UNC\\host\\...``) connects to that host over SMB or WebDAV and can hand it the user's
sign-in. Device paths (``\\\\.\\...``, ``\\\\?\\GLOBALROOT\\...``) name hardware and pipes, never a
file the user asked about. Callers check this before any filesystem call on a path from a request.
"""
from __future__ import annotations

import os
import sys
from typing import Any


def is_network_or_device_path(raw: Any) -> bool:
    """True for a Windows UNC or device path; ``\\\\?\\C:\\...`` (a local drive) is not one."""
    if sys.platform != "win32" or raw is None:
        return False
    try:
        text = os.fspath(raw)
    except TypeError:
        text = raw
    if not isinstance(text, str):
        return False
    text = text.strip().replace("/", "\\")
    if not text.startswith("\\\\"):
        return False
    if text.startswith("\\\\?\\"):
        rest = text[4:]
        return not (len(rest) >= 2 and rest[1] == ":")
    return True
