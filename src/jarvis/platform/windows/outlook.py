"""Classic Outlook through COM: which Outlook is installed, and a bounded call into its MAPI namespace.

Only the classic Outlook desktop app registers the ``Outlook.Application`` COM class. The new Outlook
(``olk.exe``) has no COM interface at all, so it is detected separately and reported, never driven.
Calls run on their own worker thread with COM initialised there and a hard time limit
(``_bounded.run_bounded``). Nothing here knows about tools or the LLM. Callers only read; this module
offers no way to send, change or delete anything.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Callable, TypeVar

from ...debug import debug_log
from ._bounded import run_bounded

T = TypeVar("T")
DEFAULT_TIMEOUT_SEC = 20.0


class OutlookUnavailable(RuntimeError):
    """Outlook could not be used. ``reason`` is ``outlook_not_installed``, ``outlook_new_only``,
    ``outlook_timeout`` or ``outlook_failed``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def install_state() -> str:
    """``classic`` when classic Outlook's COM class is registered, ``new_only`` when only the new
    Outlook is present, otherwise ``none``."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Outlook.Application\CLSID"):
            return "classic"
    except OSError:
        pass
    except ImportError:
        return "none"
    local = os.environ.get("LOCALAPPDATA", "")
    alias = os.path.join(local, "Microsoft", "WindowsApps", "olk.exe") if local else ""
    if (alias and os.path.exists(alias)) or shutil.which("olk.exe"):
        return "new_only"
    return "none"


def run_with_namespace(fn: Callable[[Any], T], timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> T:
    """``fn(namespace)`` with classic Outlook's MAPI namespace, on a COM worker thread.

    Attaches to a running Outlook, or starts it in the background. Raises ``OutlookUnavailable``."""

    def call() -> T:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            try:
                app = win32com.client.GetActiveObject("Outlook.Application")
            except Exception:  # noqa: BLE001 - not running: start it
                app = win32com.client.Dispatch("Outlook.Application")
            namespace = app.GetNamespace("MAPI")
            try:
                return fn(namespace)
            finally:
                namespace = None
                app = None
        finally:
            pythoncom.CoUninitialize()

    try:
        return run_bounded(call, timeout_sec)
    except TimeoutError as exc:
        debug_log("outlook call timed out", "outlook")
        raise OutlookUnavailable("outlook_timeout") from exc
    except ImportError as exc:
        raise OutlookUnavailable("outlook_not_installed") from exc
    except OutlookUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - COM errors carry no useful type
        debug_log(f"outlook call failed ({type(exc).__name__})", "outlook")
        raise OutlookUnavailable("outlook_failed") from exc
