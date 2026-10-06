"""Node.js for the MCP servers the setup wizard offers.

Jarvis does not bundle Node.js. This module finds an installed copy, including one installed
after Jarvis started (the running process keeps the PATH it started with), and builds the
winget command that installs the official LTS release when the user asks for it. Nothing here
runs an installer.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from jarvis.debug import debug_log

NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"
NODE_PACKAGE = "OpenJS.NodeJS.LTS"


def _system_path_entries() -> List[str]:
    """The machine and user PATH entries as Windows has them now (not as this process started)."""
    import winreg

    entries: List[str] = []
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    )
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        entries += [os.path.expandvars(part) for part in str(value).split(os.pathsep) if part.strip()]
    return entries


def _default_install_dirs() -> List[str]:
    base = os.environ.get("ProgramFiles")
    return [str(Path(base) / "nodejs")] if base else []


def _add_to_path(directory: str) -> None:
    current = os.environ.get("PATH", "")
    if directory not in current.split(os.pathsep):
        os.environ["PATH"] = os.pathsep.join(p for p in (current, directory) if p)


def node_available() -> bool:
    """Whether ``npx`` can be started.

    On Windows a copy that this process's PATH misses (installed after Jarvis started) is found
    on the current system PATH or in the default install folder, and its folder is added to this
    process's PATH so MCP servers started later, and a daemon started later, find it too.
    """
    from jarvis.tools.external.mcp_client import _resolve_command

    try:
        _resolve_command("npx")
        return True
    except Exception:
        pass
    if sys.platform != "win32":
        return False
    try:
        candidates = _system_path_entries()
    except Exception:
        candidates = []
    for directory in candidates + _default_install_dirs():
        if os.path.isfile(os.path.join(directory, "npx.cmd")):
            _add_to_path(directory)
            debug_log("node.js found outside this process's PATH; added its folder", "setup")
            return True
    return False


def winget_path() -> Optional[str]:
    """The Windows Package Manager, or None when it is not available."""
    if sys.platform != "win32":
        return None
    found = shutil.which("winget")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    candidate = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe" if local else None
    return str(candidate) if candidate and candidate.is_file() else None


def install_command() -> Optional[List[str]]:
    """The command that installs the Node.js LTS release, or None without winget.

    It runs in a hidden console, so winget must never stop to ask: the source and package
    agreements are accepted by the user's click and interactivity is off. Windows still shows
    its own permission prompt for the installer.
    """
    winget = winget_path()
    if winget is None:
        return None
    return [winget, "install", "--id", NODE_PACKAGE, "--exact", "--source", "winget",
            "--accept-source-agreements", "--accept-package-agreements", "--disable-interactivity"]
