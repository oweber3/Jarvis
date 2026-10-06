"""Builds the background Claude bridge service. ``bridge.modes`` starts, warms and stops it.

Nothing here is imported in ``local`` mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def runtime_dir() -> Path:
    """The dedicated, empty working directory of every Claude Code child."""
    from ..config import default_config_path
    return default_config_path().parent / "claude_runtime"


def check_sign_in(cfg: Any, *, auth_reader: Optional[Callable[[], Dict[str, Any]]] = None) -> Optional[str]:
    """The sign-in half of the preflight, for setup: None when Claude Code is installed and signed in
    with a Claude subscription, else ``not_found``, ``start_failed``, ``signed_out`` or ``api_key_auth``.
    Runs ``claude auth status`` only: no session starts and nothing is sent."""
    from ..debug import debug_log
    from .cli import ClaudeCliError, read_auth_status, resolve_executable, sign_in_failure

    if auth_reader is None:
        configured = str(getattr(cfg, "claude_executable", "") or "claude")
        executable = resolve_executable(configured) or configured
        auth_reader = lambda: read_auth_status(executable)  # noqa: E731
    try:
        auth = auth_reader()
    except ClaudeCliError as exc:
        failure = "not_found" if exc.reason == "not_found" else "start_failed"
    else:
        failure = sign_in_failure(auth)
    debug_log(f"claude sign-in check: {failure or 'ready'}", "claude")
    return failure


def create_service(cfg: Any, *, workdir: Optional[Path] = None,
                   session_factory: Optional[Callable[[str, List[str]], Any]] = None,
                   auth_reader: Optional[Callable[[], Dict[str, Any]]] = None) -> Any:
    """The Claude bridge service with its own runtime directory. Starts no process."""
    from .cli import ClaudeSession, read_auth_status, resolve_executable
    from .service import ClaudeBridgeService

    directory = Path(workdir) if workdir else runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    configured = str(getattr(cfg, "claude_executable", "") or "claude")
    executable = resolve_executable(configured) or configured

    def sessions(session_id: str, args: List[str]) -> Any:
        return ClaudeSession(executable, args, session_id=session_id, cwd=directory)

    return ClaudeBridgeService(cfg, session_factory or sessions,
                               auth_reader=auth_reader or (lambda: read_auth_status(executable)))
