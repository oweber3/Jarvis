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


_CHECK_TIMEOUT_SEC = 15.0


def check_sign_in(cfg: Any, *, client_factory: Callable[[Any, Path], Any] = build_client,
                  workdir: Optional[Path] = None) -> Optional[str]:
    """The sign-in half of the preflight, for setup: None when Codex is installed and signed in with
    ChatGPT, else ``not_found``, ``start_failed``, ``unsupported``, ``signed_out`` or ``api_key_auth``.
    Starts the child, reads the account (``account/read``) and stops it: no thread or turn starts and
    nothing is sent."""
    from ..debug import debug_log
    from .app_server import AppServerError, account_failure

    directory = Path(workdir) if workdir else runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    client = client_factory(cfg, directory)
    try:
        client.start()
        failure = account_failure(client.request("account/read", {}, _CHECK_TIMEOUT_SEC))
    except AppServerError as exc:
        if exc.reason == "not_found":
            failure = "not_found"
        else:
            failure = "unsupported" if exc.reason == "rpc_error" else "start_failed"
    finally:
        client.close()
    debug_log(f"codex sign-in check: {failure or 'ready'}", "codex")
    return failure


def create_service(cfg: Any, *, client_factory: Callable[[Any, Path], Any] = build_client,
                   workdir: Optional[Path] = None) -> Any:
    """The Codex bridge service with its own runtime directory. Starts no process."""
    from .service import BridgeService

    directory = Path(workdir) if workdir else runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return BridgeService(cfg, client_factory(cfg, directory), runtime_dir=directory)
