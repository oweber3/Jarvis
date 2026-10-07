"""Client for one Jarvis-owned headless Claude Code process (``claude -p``, stream-json in and out).

One process is one session. Newline-delimited JSON over pipes: a reader thread parses stdout and
queues the CLI's control requests, the responses to Jarvis's own control requests and the session's
messages; it never executes anything. The thread that owns the session handles every event. stderr
is drained separately and never logged. See ``claude_bridge.spec.md``.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..debug import debug_log

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_EVENTS = 2000
MCP_SERVER = "jarvis"
_READ_CHUNK = 65536
_ERROR_MESSAGE_CHARS = 300
_AUTH_TIMEOUT_SEC = 20.0
# The only sign-in Claude mode uses: a Claude subscription, never an API key.
SUBSCRIPTION_AUTH = "claude.ai"

# Analytics, error reporting, update checks, memory files and auto-memory are off for the child only;
# the user's Claude Code settings are never changed.
CHILD_ENVIRONMENT = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "NO_COLOR": "1",
}
# Session-scoped settings: other Claude sessions on the PC cannot message this one.
SESSION_SETTINGS = {"crossSessionInbound": "refuse"}


class ClaudeCliError(Exception):
    """A failure talking to the child. ``reason`` is one of not_found, start_failed, timeout or closed."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason
        self.message = message[:_ERROR_MESSAGE_CHARS]


def child_environment(base: Mapping[str, str]) -> Dict[str, str]:
    """The child's environment: the user's, without inherited Anthropic or Claude Code variables.

    Removing them means an API key, a proxy URL or a parent Claude Code session in Jarvis's own
    environment can never change how the child signs in or whom it talks to; only the CLI's own
    sign-in is used. ``CLAUDE_CONFIG_DIR`` is kept because it locates that sign-in.
    """
    env = {key: value for key, value in base.items()
           if not (key.upper().startswith("ANTHROPIC_")
                   or (key.upper().startswith("CLAUDE") and key.upper() != "CLAUDE_CONFIG_DIR"))}
    env.update(CHILD_ENVIRONMENT)
    return env


def session_args(*, model: str, effort: Optional[str], max_turns: int, system_prompt: str,
                 answer_schema: Mapping[str, Any], session_id: str) -> List[str]:
    """Arguments for one isolated session. Every flag is verified against ``claude --help`` 2.1.288
    (``--max-turns`` is accepted but not listed there)."""
    args = [
        "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--tools", "",
        "--strict-mcp-config",
        "--mcp-config", json.dumps({"mcpServers": {MCP_SERVER: {"type": "sdk", "name": MCP_SERVER}}}),
        "--setting-sources", "",
        "--settings", json.dumps(SESSION_SETTINGS),
        "--no-session-persistence",
        "--disable-slash-commands",
        "--permission-mode", "dontAsk",
        "--allowedTools", f"mcp__{MCP_SERVER}__jarvis_execute",
        "--system-prompt", system_prompt,
        "--json-schema", json.dumps(answer_schema, separators=(",", ":")),
        "--model", model,
        "--max-turns", str(max(1, int(max_turns))),
        "--session-id", session_id,
    ]
    if effort:
        args += ["--effort", effort]
    return args


def resolve_executable(executable: str) -> Optional[str]:
    """The Claude Code executable to start, or None when it cannot be found."""
    name = (executable or "").strip() or "claude"
    if os.path.dirname(name):
        return name if os.path.isfile(name) else None
    found = shutil.which(name)
    if found:
        return found
    if name.lower() not in ("claude", "claude.exe"):
        return None
    # The native installer's location for the current user.
    candidate = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    return str(candidate) if candidate.is_file() else None


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


def read_auth_status(executable: str, *, runner: Callable[..., Any] = subprocess.run,
                     timeout_sec: float = _AUTH_TIMEOUT_SEC) -> Dict[str, Any]:
    """``claude auth status --json``: whether and how the CLI is signed in. No inference, no tokens."""
    try:
        done = runner([executable, "auth", "status", "--json"], capture_output=True, timeout=timeout_sec,
                      env=child_environment(os.environ), creationflags=_creationflags())
    except FileNotFoundError:
        raise ClaudeCliError("not_found") from None
    except subprocess.TimeoutExpired:
        raise ClaudeCliError("timeout", "auth status") from None
    except OSError as exc:
        raise ClaudeCliError("start_failed", type(exc).__name__) from None
    try:
        data = json.loads(done.stdout or b"{}")
    except ValueError:
        raise ClaudeCliError("start_failed", "auth status output") from None
    if not isinstance(data, dict):
        raise ClaudeCliError("start_failed", "auth status output")
    return {key: data.get(key) for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}


def sign_in_failure(auth: Mapping[str, Any]) -> Optional[str]:
    """``signed_out`` or ``api_key_auth`` for an ``auth status`` that Claude mode cannot use, else None."""
    if not auth.get("loggedIn"):
        return "signed_out"
    if auth.get("authMethod") != SUBSCRIPTION_AUTH:
        return "api_key_auth"
    return None


class ClaudeSession:
    """One ``claude -p`` child process: one session, owned by Jarvis from start to close."""

    def __init__(
        self,
        executable: str,
        args: Sequence[str],
        *,
        session_id: str,
        cwd: Optional[Path] = None,
        process_factory: Callable[..., Any] = subprocess.Popen,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ):
        self.session_id = session_id
        self._executable = executable
        self._args = list(args)
        self._cwd = Path(cwd) if cwd else None
        self._factory = process_factory
        self._max_bytes = max_message_bytes
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._proc: Any = None
        self._closed = True
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    @property
    def alive(self) -> bool:
        with self._lock:
            return not self._closed and self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._proc, "pid", None)

    def start(self) -> None:
        """Launch the child. The caller then sends ``initialize`` and handles the handshake events."""
        kwargs: Dict[str, Any] = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      cwd=str(self._cwd) if self._cwd else None,
                                      env=child_environment(os.environ))
        if sys.platform == "win32":
            kwargs["creationflags"] = _creationflags()
        try:
            proc = self._factory([self._executable, *self._args], **kwargs)
        except FileNotFoundError:
            raise ClaudeCliError("not_found") from None
        except OSError as exc:
            raise ClaudeCliError("start_failed", type(exc).__name__) from None
        with self._lock:
            self._proc, self._closed = proc, False
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True,
                         name=f"claude-out-{self.session_id[:8]}").start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name=f"claude-err-{self.session_id[:8]}").start()
        debug_log(f"claude session {self.session_id[:8]} started", "claude")

    # -- Jarvis -> CLI --------------------------------------------------------------

    def send_control(self, subtype: str, payload: Optional[Dict[str, Any]] = None) -> str:
        """Send a control request. Its response arrives as a ``control_response`` event."""
        request_id = f"jarvis-{uuid.uuid4().hex}"
        self._write({"type": "control_request", "request_id": request_id,
                     "request": {"subtype": subtype, **(payload or {})}})
        return request_id

    def send_user_text(self, text: str) -> None:
        self._write({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def respond(self, request_id: Any, response: Dict[str, Any]) -> None:
        self._write({"type": "control_response",
                     "response": {"subtype": "success", "request_id": request_id, "response": response}})

    def respond_error(self, request_id: Any, message: str) -> None:
        self._write({"type": "control_response",
                     "response": {"subtype": "error", "request_id": request_id, "error": message}})

    def poll_event(self, timeout_sec: float) -> Optional[Dict[str, Any]]:
        try:
            return self._events.get(timeout=max(0.0, timeout_sec))
        except queue.Empty:
            return None

    def close(self, timeout_sec: float = 3.0) -> None:
        """Stop this child: end its input, wait a bounded time, then terminate and kill. Safe to repeat."""
        with self._lock:
            proc = self._proc
        self._mark_closed("closed_by_client", queue_event=False)
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
                    debug_log("claude child did not exit after kill", "claude")
        except Exception:
            pass

    def _write(self, message: Dict[str, Any]) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            proc, closed = self._proc, self._closed
        if closed or proc is None:
            raise ClaudeCliError("closed")
        try:
            with self._write_lock:
                proc.stdin.write(data)
                proc.stdin.flush()
        except (OSError, ValueError):
            self._mark_closed("exited")
            raise ClaudeCliError("closed") from None

    def _mark_closed(self, reason: str, queue_event: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if queue_event:
            self._events.put({"kind": "closed", "reason": reason})
        debug_log(f"claude session {self.session_id[:8]} closed ({reason})", "claude")

    # -- reader threads ----------------------------------------------------------------

    def _read_stdout(self, proc: Any) -> None:
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
                        self._dispatch(line)
                if len(buffer) > self._max_bytes:
                    raise _ProtocolError("oversized")
        except _ProtocolError as exc:
            reason = exc.reason
        except Exception as exc:
            debug_log(f"claude reader stopped: {type(exc).__name__}", "claude")
        self._mark_closed(reason)
        if reason != "exited":
            try:
                proc.kill()
            except Exception:
                pass

    def _dispatch(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            raise _ProtocolError("malformed") from None
        if not isinstance(message, dict):
            raise _ProtocolError("malformed")
        kind = message.get("type")
        if kind == "control_response":
            response = message.get("response") or {}
            self._events.put({"kind": "control_response", "request_id": response.get("request_id"),
                              "response": response})
            return
        if kind == "control_request":
            self._events.put({"kind": "control_request", "request_id": message.get("request_id"),
                              "request": message.get("request") or {}})
            return
        if kind == "control_cancel_request":
            return
        # Progress messages beyond the bound are dropped; the turn's result never is.
        if kind != "result" and self._events.qsize() >= MAX_EVENTS:
            return
        self._events.put({"kind": "message", "type": kind, "message": message})

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
