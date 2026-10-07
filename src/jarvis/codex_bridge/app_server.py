"""Client for one Jarvis-owned ``codex app-server --listen stdio://`` child process.

Newline-delimited JSON-RPC over pipes. A reader thread parses stdout and queues server
requests and notifications; it never executes anything. stderr is drained separately and
never logged. See ``app_server.spec.md``.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..debug import debug_log

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_EVENTS = 2000
_READ_CHUNK = 65536
_ERROR_MESSAGE_CHARS = 300

_FEATURES_OFF = (
    "shell_tool", "view_image", "sleep_tool", "multi_agent", "apps", "plugins",
    "browser_use", "computer_use", "image_generation", "tool_suggest", "skill_search",
    "skill_mcp_dependency_install", "hooks", "goals", "in_app_browser", "memories",
    "workspace_dependencies", "remote_plugin", "collaboration_modes",
)

# Process-scoped configuration that leaves the dynamic tools as the only action path.
ISOLATION_OVERRIDES = tuple(f"features.{name}=false" for name in _FEATURES_OFF) + (
    'web_search="disabled"',
    'history.persistence="none"',
    "notify=[]",
    "analytics.enabled=false",
    "project_doc_max_bytes=0",
    "include_apps_instructions=false",
    "include_environment_context=false",
    "include_permissions_instructions=false",
    "include_collaboration_mode_instructions=false",
    "check_for_update_on_startup=false",
    "agents.max_threads=1",
)

# Streaming notifications Jarvis never reads; suppressing them keeps the event queue small.
OPTED_OUT_NOTIFICATIONS = (
    "item/agentMessage/delta", "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta", "item/plan/delta", "item/commandExecution/outputDelta",
    "item/fileChange/outputDelta", "command/exec/outputDelta", "process/outputDelta",
    "thread/tokenUsage/updated", "account/rateLimits/updated", "turn/diff/updated",
)


class AppServerError(Exception):
    """A failure talking to the child. ``reason`` is one of not_found, start_failed, timeout,
    closed or rpc_error."""

    def __init__(self, reason: str, message: str = "", code: Any = None):
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason
        self.message = message[:_ERROR_MESSAGE_CHARS]
        self.code = code


def thread_isolation_config(effective_config: Mapping[str, Any]) -> Dict[str, bool]:
    """Per-thread overrides disabling every configured MCP server and plugin by name."""
    out: Dict[str, bool] = {}
    for section in ("mcp_servers", "plugins"):
        entries = effective_config.get(section) if isinstance(effective_config, Mapping) else None
        if isinstance(entries, Mapping):
            for name in entries:
                out[f"{section}.{name}.enabled"] = False
    return out


def account_failure(response: Mapping[str, Any]) -> Optional[str]:
    """``signed_out`` or ``api_key_auth`` for an ``account/read`` response without a ChatGPT sign-in,
    else None."""
    account = response.get("account")
    if not isinstance(account, dict):
        return "signed_out" if response.get("requiresOpenaiAuth", True) else "api_key_auth"
    return None if account.get("type") == "chatgpt" else "api_key_auth"


def resolve_executable(executable: str) -> Optional[str]:
    """The Codex executable to start, or None when it cannot be found."""
    name = (executable or "").strip() or "codex"
    if os.path.dirname(name):
        return name if os.path.isfile(name) else None
    found = shutil.which(name)
    if found:
        return found
    if name.lower() not in ("codex", "codex.exe"):
        return None
    if sys.platform == "darwin":
        found = _macos_install()
        debug_log(f"codex executable {'found in a standard macOS location' if found else 'not found'}", "codex")
        return found
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    candidates = list((Path(base) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
    if not candidates:
        return None
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


_FS_ROOT = Path("/")
# Desktop apps that bundle the Codex CLI: the ChatGPT app, and the Codex app it replaced.
_MACOS_APPS = ("ChatGPT.app", "Codex.app")


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _macos_install() -> Optional[str]:
    """A Codex CLI in a standard macOS location. Apps started from the Dock or Finder get a short
    ``PATH`` without Homebrew or the user's bin folders. Standalone installs come before the copy a
    desktop app bundles."""
    home = Path.home()
    for folder in (_FS_ROOT / "opt" / "homebrew" / "bin", _FS_ROOT / "usr" / "local" / "bin",
                   home / ".local" / "bin", home / ".npm-global" / "bin"):
        if _is_executable(folder / "codex"):
            return str(folder / "codex")
    for applications in (_FS_ROOT / "Applications", home / "Applications"):
        for app in _MACOS_APPS:
            resources = applications / app / "Contents" / "Resources"
            for candidate in (resources / "codex", *sorted(resources.glob("*/codex"))):
                if _is_executable(candidate):
                    return str(candidate)
    return None


class _Pending:
    __slots__ = ("done", "response")

    def __init__(self):
        self.done = threading.Event()
        self.response: Optional[Dict[str, Any]] = None


class AppServerClient:
    """One child process at a time; a restart is a new generation."""

    def __init__(
        self,
        executable: str,
        *,
        cwd: Optional[Path] = None,
        config_overrides: Sequence[str] = ISOLATION_OVERRIDES,
        process_factory: Callable[..., Any] = subprocess.Popen,
        client_version: str = "1",
        start_timeout_sec: float = 15.0,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ):
        self._executable = executable
        self._cwd = Path(cwd) if cwd else None
        self._overrides = tuple(config_overrides)
        self._factory = process_factory
        self._client_version = client_version
        self._start_timeout = start_timeout_sec
        self._max_bytes = max_message_bytes
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._proc: Any = None
        self._generation = 0
        self._closed = True
        self._next_id = 0
        self._pending: Dict[Any, _Pending] = {}
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    # -- lifecycle ---------------------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def alive(self) -> bool:
        with self._lock:
            return not self._closed and self._proc is not None and self._proc.poll() is None

    def start(self) -> Dict[str, Any]:
        """Start and initialise the child unless it is already running."""
        with self._lock:
            if self.alive:
                return {}
            self._launch()
            generation = self._generation
        try:
            result = self.request("initialize", {
                "clientInfo": {"name": "jarvis", "title": "Jarvis", "version": self._client_version},
                "capabilities": {"experimentalApi": True,
                                 "optOutNotificationMethods": list(OPTED_OUT_NOTIFICATIONS)},
            }, self._start_timeout)
            self.notify("initialized")
        except AppServerError as exc:
            debug_log(f"app-server generation {generation} failed to initialise ({exc.reason})", "codex")
            self._stop_process()
            raise AppServerError("timeout" if exc.reason == "timeout" else "start_failed", exc.message) from None
        debug_log(f"app-server generation {generation} initialised", "codex")
        return result

    def _launch(self) -> None:
        args: List[str] = [self._executable, "app-server", "--listen", "stdio://"]
        for override in self._overrides:
            args += ["-c", override]
        env = dict(os.environ, RUST_LOG="warn", NO_COLOR="1")
        kwargs: Dict[str, Any] = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      cwd=str(self._cwd) if self._cwd else None, env=env)
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            proc = self._factory(args, **kwargs)
        except FileNotFoundError:
            raise AppServerError("not_found") from None
        except OSError as exc:
            raise AppServerError("start_failed", type(exc).__name__) from None
        self._generation += 1
        self._proc = proc
        self._closed = False
        generation = self._generation
        threading.Thread(target=self._read_stdout, args=(proc, generation), daemon=True,
                         name=f"codex-app-server-out-{generation}").start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name=f"codex-app-server-err-{generation}").start()
        debug_log(f"app-server generation {generation} started", "codex")

    def close(self, timeout_sec: float = 5.0) -> None:
        """Stop the child this client started. Bounded; safe to repeat."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return
            self._mark_closed(self._generation, "closed_by_client", queue_event=False)
        self._stop_process(proc, timeout_sec)

    def _stop_process(self, proc: Any = None, timeout_sec: float = 5.0) -> None:
        with self._lock:
            proc = proc or self._proc
            self._mark_closed(self._generation, "closed_by_client", queue_event=False)
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    debug_log("app-server child did not exit after kill", "codex")
        except Exception:
            pass

    def _mark_closed(self, generation: int, reason: str, queue_event: bool = True) -> None:
        """Close ``generation`` once: fail pending requests and queue one closed event."""
        with self._lock:
            if generation != self._generation or self._closed:
                return
            self._closed = True
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot.response = {"_closed": reason}
            slot.done.set()
        if queue_event:
            self._events.put({"generation": generation, "kind": "closed", "reason": reason})
        debug_log(f"app-server generation {generation} closed ({reason})", "codex")

    # -- requests ----------------------------------------------------------------

    def request(self, method: str, params: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        with self._lock:
            if self._closed:
                raise AppServerError("closed")
            self._next_id += 1
            rpc_id = self._next_id
            slot = _Pending()
            self._pending[rpc_id] = slot
        self._write({"id": rpc_id, "method": method, "params": params})
        if not slot.done.wait(timeout_sec):
            with self._lock:
                self._pending.pop(rpc_id, None)
            raise AppServerError("timeout", method)
        response = slot.response or {}
        if "_closed" in response:
            raise AppServerError("closed", response["_closed"])
        if "error" in response:
            err = response.get("error") or {}
            raise AppServerError("rpc_error", str(err.get("message", "")), err.get("code"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        message: Dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def respond(self, rpc_id: Any, result: Dict[str, Any]) -> None:
        self._write({"id": rpc_id, "result": result})

    def respond_error(self, rpc_id: Any, code: int, message: str) -> None:
        self._write({"id": rpc_id, "error": {"code": code, "message": message}})

    def poll_event(self, timeout_sec: float) -> Optional[Dict[str, Any]]:
        try:
            return self._events.get(timeout=max(0.0, timeout_sec))
        except queue.Empty:
            return None

    def _write(self, message: Dict[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            proc, generation, closed = self._proc, self._generation, self._closed
        if closed or proc is None:
            raise AppServerError("closed")
        try:
            with self._write_lock:
                proc.stdin.write(data)
                proc.stdin.flush()
        except (OSError, ValueError):
            self._mark_closed(generation, "exited")
            raise AppServerError("closed") from None

    # -- reader threads ------------------------------------------------------------

    def _read_stdout(self, proc: Any, generation: int) -> None:
        buffer = bytearray()
        reason = "exited"
        stream = proc.stdout
        read = getattr(stream, "read1", stream.read)
        try:
            while True:
                chunk = read(_READ_CHUNK)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffer[:newline]).strip()
                    del buffer[:newline + 1]
                    if len(line) > self._max_bytes:
                        raise _ProtocolError("oversized")
                    if line:
                        self._dispatch(line, generation)
                if len(buffer) > self._max_bytes:
                    raise _ProtocolError("oversized")
        except _ProtocolError as exc:
            reason = exc.reason
        except Exception as exc:
            debug_log(f"app-server reader stopped: {type(exc).__name__}", "codex")
        self._mark_closed(generation, reason)
        if reason != "exited":
            try:
                proc.kill()
            except Exception:
                pass

    def _dispatch(self, line: bytes, generation: int) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            raise _ProtocolError("malformed") from None
        if not isinstance(message, dict):
            raise _ProtocolError("malformed")
        method = message.get("method")
        if method is None:
            with self._lock:
                slot = self._pending.pop(message.get("id"), None) if generation == self._generation else None
            if slot is not None:
                slot.response = message
                slot.done.set()
            return
        kind = "request" if "id" in message else "notification"
        if kind == "notification" and self._events.qsize() >= MAX_EVENTS:
            return
        self._events.put({"generation": generation, "kind": kind, "method": method,
                          "params": message.get("params") or {}, "id": message.get("id")})

    @staticmethod
    def _read_stderr(proc: Any) -> None:
        """Drain and discard diagnostics so a full pipe never blocks the child."""
        stream = proc.stderr
        read = getattr(stream, "read1", stream.read)
        try:
            while read(_READ_CHUNK):
                pass
        except Exception:
            pass


class _ProtocolError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
