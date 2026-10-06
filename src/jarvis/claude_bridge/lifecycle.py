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
