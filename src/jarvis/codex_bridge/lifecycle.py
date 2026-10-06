"""Builds the background Codex bridge service. ``bridge.modes`` starts, warms and stops it.

Nothing here is imported in ``local`` mode.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional


def runtime_dir() -> Path:
    """The dedicated, empty working directory of the Codex child."""
    from ..config import default_config_path
    return default_config_path().parent / "codex_runtime"


def build_client(cfg: Any, workdir: Path) -> Any:
    """The app-server client for the configured executable."""
    from .app_server import AppServerClient, resolve_executable

    configured = str(getattr(cfg, "codex_executable", "") or "codex")
    return AppServerClient(resolve_executable(configured) or configured, cwd=workdir)


def create_service(cfg: Any, *, client_factory: Callable[[Any, Path], Any] = build_client,
                   workdir: Optional[Path] = None) -> Any:
    """The Codex bridge service with its own runtime directory. Starts no process."""
    from .service import BridgeService

    directory = Path(workdir) if workdir else runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return BridgeService(cfg, client_factory(cfg, directory), runtime_dir=directory)
