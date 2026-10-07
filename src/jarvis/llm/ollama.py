"""Ollama HTTP backend with stable, backend-owned context and residency settings."""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Union

import http.client
import json
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

import requests

from ..debug import debug_log
from .errors import RequestCancelled, is_timeout_error
from .backend import LLMBackend, ToolsNotSupportedError, strip_nonstandard_message_fields


def check_version(base_url: str, timeout: float = 5.0) -> tuple[bool, str | None]:
    """Probe ``GET /api/version`` and return ``(True, version_str)`` if the
    endpoint responds as an Ollama server, or ``(False, None)`` on failure or
    non-Ollama response."""
    try:
        resp = requests.get(f"{base_url}/api/version", timeout=timeout)
        if resp.status_code != 200:
            return False, None
        data = resp.json()
        if not isinstance(data, dict):
            return False, None
        version = data.get("version")
        if not isinstance(version, str) or not version:
            return False, None
        return True, version
    except Exception:
        return False, None


def extract_text_from_response(data: Dict[str, Any]) -> Optional[str]:
    """Extract text from an LLM chat response across known shapes.

    Handles Ollama's ``message.content`` shape and the OpenAI-style
    ``choices[0].message.content`` / ``choices[0].text`` fallbacks so
    callers do not need to special-case responses that come back from
    OpenAI-compatible runtimes proxied through Ollama.
    """
    if "message" in data and isinstance(data["message"], dict):
        content = data["message"].get("content")
        if isinstance(content, str):
            return content

    if "choices" in data and isinstance(data["choices"], list) and len(data["choices"]) > 0:
        choice = data["choices"][0]
        if isinstance(choice, dict):
            if "message" in choice and isinstance(choice["message"], dict):
                content = choice["message"].get("content")
                if isinstance(content, str):
                    return content
            elif "text" in choice:
                content = choice["text"]
                if isinstance(content, str):
                    return content

    if "content" in data:
        content = data["content"]
        if isinstance(content, str):
            return content

    return None


def _drop_connection(sock: Optional[socket.socket]) -> None:
    """Close a socket now, even while another thread is blocked reading it, so the server sees the
    connection reset and stops generating. Closing the socket object alone is not enough: the response's
    buffered reader holds it open, so its handle is released directly."""
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        socket.socket(fileno=sock.detach()).close()
    except OSError:
        pass


# (base URL, model) -> whether Ollama reports the model's ``vision`` capability. Only answers Ollama
# gave are kept, so an unreachable server or a model not yet pulled is asked again next time.
_VISION: Dict[tuple, bool] = {}
_VISION_TIMEOUT_SEC = 3.0


class OllamaBackend(LLMBackend):
    """:class:`LLMBackend` implementation that talks to a local Ollama server."""

    def __init__(self, base_url: str, *, num_ctx: int = 8192, keep_alive: Union[str, int] = "30m") -> None:
        self._base_url = base_url.rstrip("/")
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive

    def _apply_request_shape(self, payload: Dict[str, Any]) -> None:
        """Apply this backend target's load and residency policy to every completion."""
        payload.setdefault("options", {})["num_ctx"] = self._num_ctx
        payload["options"].pop("keep_alive", None)
        payload["keep_alive"] = self._keep_alive
        payload.setdefault("think", False)

    @property
    def base_url(self) -> str:
        return self._base_url

    # ── chat ───────────────────────────────────────────────────────────

    def direct(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        timeout_sec: float = 10.0,
        thinking: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """Direct LLM call without temporal context, location, or other
        ``ask_coach`` features.

        Context size and model residency come from the backend target.

        ``temperature`` is forwarded to Ollama when set. Pass ``0.0``
        for classification / extraction calls where determinism beats
        creativity — Ollama defaults to ~0.8 otherwise, which can
        flake small models on rule-following tasks (e.g. the knowledge
        extractor's banned-form list).

        ``max_tokens`` maps to Ollama's ``num_predict``, capping the
        total generated tokens (including reasoning). Essential for
        classification calls where small reasoning models otherwise
        loop endlessly.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        options: Dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": messages,
            "stream": False,
            "cache_prompt": True,
            "options": options,
            "think": thinking,
        }

        self._apply_request_shape(payload)
        try:
            with requests.post(
                f"{self._base_url}/api/chat", json=payload, timeout=timeout_sec
            ) as resp:
                resp.raise_for_status()
                data = resp.json()

            if isinstance(data, dict):
                content = extract_text_from_response(data)
                if isinstance(content, str) and content.strip():
                    return content
                debug_log(
                    f"OllamaBackend.direct: empty content from response keys={list(data.keys())}",
                    "llm",
                )
        except requests.exceptions.Timeout:
            debug_log(f"OllamaBackend.direct: timeout after {timeout_sec}s", "llm")
            return None
        except Exception as e:
            debug_log(f"OllamaBackend.direct: request failed — {e}", "llm")
            return None

        return None

    def streaming(
        self,
        chat_model: str,
        system_prompt: str,
        user_content: str,
        on_token: Optional[Callable[[str], None]] = None,
        timeout_sec: float = 30.0,
        thinking: bool = False,
    ) -> Optional[str]:
        """Streaming LLM call that invokes ``on_token`` for each token
        received. Returns the complete response text, or ``None`` on
        error / empty stream.

        Uses ``with requests.post(...)`` so the streaming response (and
        the underlying TCP connection) is released even if iteration
        exits early via an exception or the caller stops consuming.
        Without this, an aborted stream pinned the connection until GC,
        which could happen many turns later under sustained reply
        load.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": messages,
            "stream": True,
            "cache_prompt": True,
            "think": thinking,
        }

        self._apply_request_shape(payload)
        try:
            with requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=timeout_sec,
                stream=True,
            ) as resp:
                resp.raise_for_status()

                full_response: List[str] = []
                for line in resp.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            if "message" in data and isinstance(data["message"], dict):
                                content = data["message"].get("content", "")
                                if content:
                                    full_response.append(content)
                                    if on_token:
                                        on_token(content)
                        except json.JSONDecodeError:
                            continue

                result = "".join(full_response)
                return result if result.strip() else None

        except requests.exceptions.Timeout:
            return None
        except Exception:
            return None

    def chat(
        self,
        chat_model: str,
        messages: List[Dict[str, Any]],
        timeout_sec: float = 30.0,
        extra_options: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        thinking: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Send an arbitrary messages array to Ollama and return the
        raw response JSON. Caller is responsible for interpreting
        assistant content (including JSON / tool calls).

        Context size and residency are shared with direct, streaming and warmup.
        """
        payload = self._chat_payload(chat_model, messages, extra_options, tools, thinking)

        try:
            with requests.post(
                f"{self._base_url}/api/chat", json=payload, timeout=timeout_sec
            ) as resp:
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict):
                return data
        except requests.exceptions.Timeout:
            print(f"  ⏱️ LLM request timed out (configured timeout: {timeout_sec:g}s)", flush=True)
            return None
        except requests.exceptions.ConnectionError as exc:
            if is_timeout_error(exc):
                debug_log("chat response read timed out (wrapped transport timeout)", "llm")
                print(f"  ⏱️ LLM request timed out (configured timeout: {timeout_sec:g}s)", flush=True)
                return None
            # Bubble out so callers (e.g. the intent judge) can distinguish
            # "server unreachable" from a transient error and apply their own
            # back-off policy.
            print("  ❌ LLM connection error", flush=True)
            raise
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and tools:
                raise ToolsNotSupportedError(
                    f"Model {chat_model!r} returned HTTP 400 — native tools API not supported"
                )
            status = e.response.status_code if e.response is not None else "?"
            print(f"  ❌ LLM HTTP error (status {status})", flush=True)
            return None
        except Exception as e:
            print(f"  ❌ LLM error ({type(e).__name__})", flush=True)
            return None

        return None

    def _chat_payload(
        self,
        chat_model: str,
        messages: List[Dict[str, Any]],
        extra_options: Optional[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        thinking: bool,
    ) -> Dict[str, Any]:
        sanitised = strip_nonstandard_message_fields(messages, keep=frozenset({"images"}))
        payload: Dict[str, Any] = {
            "model": chat_model,
            "messages": sanitised,
            "stream": False,
            "cache_prompt": True,
            "options": {},
            "think": thinking,
        }
        # Generation options are translated here; backend load settings win.
        if extra_options and isinstance(extra_options, dict):
            for key, value in extra_options.items():
                if key in {"keep_alive", "format", "think"}:
                    payload[key] = value
                elif key == "max_tokens":
                    payload["options"]["num_predict"] = int(value)
                elif key == "options" and isinstance(value, dict):
                    for inner_key, inner_value in value.items():
                        if inner_key == "max_tokens":
                            payload["options"]["num_predict"] = int(inner_value)
                        else:
                            payload["options"][inner_key] = inner_value
                else:
                    payload["options"][key] = value

        self._apply_request_shape(payload)
        if tools and isinstance(tools, list) and len(tools) > 0:
            payload["tools"] = tools
        return payload

    def chat_cancellable(
        self,
        chat_model: str,
        messages: List[Dict[str, Any]],
        cancel: threading.Event,
        timeout_sec: float = 30.0,
        extra_options: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        thinking: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """``chat`` that Stop can end: the request is streamed, and the moment ``cancel`` is set the connection
        is shut down (which makes Ollama stop generating) and the caller is released. The pieces are put back
        together as the single response ``chat`` returns. ``timeout_sec`` bounds the whole call, as it does
        for ``chat``."""
        if cancel.is_set():
            raise RequestCancelled()
        payload = self._chat_payload(chat_model, messages, extra_options, tools, thinking)
        payload["stream"] = True
        target = urlsplit(f"{self._base_url}/api/chat")
        connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(target.hostname, target.port, timeout=timeout_sec)
        outcome: Dict[str, Any] = {}
        held: Dict[str, Any] = {}
        finished = threading.Event()

        # A blocked network read cannot be woken from another thread on every platform, so the read runs
        # on a helper thread and this one waits on the Stop signal.
        def read() -> None:
            try:
                outcome["value"] = self._stream_chat(connection, held, target.path, payload, tools, chat_model, timeout_sec)
            except BaseException as exc:  # handed to the waiting caller below
                outcome["error"] = exc
            finally:
                finished.set()
                connection.close()

        threading.Thread(target=read, daemon=True, name="llm-chat-stream").start()
        while not finished.wait(0.05):
            if cancel.is_set():
                _drop_connection(held.get("sock"))
                debug_log("chat call stopped, connection dropped", "llm")
                raise RequestCancelled()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    def _stream_chat(self, connection, held: Dict[str, Any], path: str, payload: Dict[str, Any], tools,
                     chat_model: str, timeout_sec: float) -> Optional[Dict[str, Any]]:
        """The streamed request itself, with the failures ``chat`` reports. The socket is put in ``held``
        because the connection forgets it once a response that ends the connection begins."""
        deadline = time.monotonic() + timeout_sec
        try:
            connection.connect()
            held["sock"] = connection.sock
            connection.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                if response.status == 400 and tools:
                    raise ToolsNotSupportedError(
                        f"Model {chat_model!r} returned HTTP 400 — native tools API not supported"
                    )
                print(f"  ❌ LLM HTTP error (status {response.status})", flush=True)
                return None
            return self._read_chat_stream(response, connection, deadline, timeout_sec)
        except ToolsNotSupportedError:
            raise
        except (socket.timeout, TimeoutError):
            print(f"  ⏱️ LLM request timed out (configured timeout: {timeout_sec:g}s)", flush=True)
            return None
        except (ConnectionError, socket.gaierror) as exc:
            print("  ❌ LLM connection error", flush=True)
            raise requests.exceptions.ConnectionError(type(exc).__name__) from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            print(f"  ❌ LLM error ({type(exc).__name__})", flush=True)
            return None

    @staticmethod
    def _read_chat_stream(response, connection, deadline: float, timeout_sec: float) -> Optional[Dict[str, Any]]:
        """Put a streamed chat answer back together: the pieces of content and thinking, the tool calls, and
        the closing chunk's fields. ``None`` when the stream ends without its closing chunk."""
        content: List[str] = []
        thinking: List[str] = []
        tool_calls: List[Any] = []
        role = "assistant"
        final: Optional[Dict[str, Any]] = None
        while final is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"  ⏱️ LLM request timed out (configured timeout: {timeout_sec:g}s)", flush=True)
                return None
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            line = response.readline()
            if not line:
                return None
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            if chunk.get("error"):
                print("  ❌ LLM error (the model reported a failure)", flush=True)
                return None
            message = chunk.get("message")
            if isinstance(message, dict):
                role = message.get("role") or role
                if isinstance(message.get("content"), str):
                    content.append(message["content"])
                if isinstance(message.get("thinking"), str):
                    thinking.append(message["thinking"])
                if isinstance(message.get("tool_calls"), list):
                    tool_calls.extend(message["tool_calls"])
            if chunk.get("done"):
                final = chunk
        assembled: Dict[str, Any] = {"role": role, "content": "".join(content)}
        if thinking:
            assembled["thinking"] = "".join(thinking)
        if tool_calls:
            assembled["tool_calls"] = tool_calls
        return {**{k: v for k, v in final.items() if k != "message"}, "message": assembled}

    # ── embeddings & discovery ────────────────────────────────────────

    def embed(
        self,
        text: str,
        model: str,
        timeout_sec: float = 15.0,
    ) -> Optional[List[float]]:
        """Embed ``text`` via Ollama's ``/api/embeddings``."""
        try:
            resp = requests.post(
                f"{self._base_url}/api/embeddings",
                json={"model": model, "prompt": text},
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data.get("embedding")
            if isinstance(vec, list):
                return [float(x) for x in vec]
        except Exception:
            return None
        return None

    def supports_images(self, chat_model: str) -> bool:
        """Read once per model from ``POST /api/show`` (its ``capabilities`` list)."""
        key = (self._base_url, chat_model)
        if key in _VISION:
            return _VISION[key]
        try:
            resp = requests.post(f"{self._base_url}/api/show", json={"model": chat_model},
                                 timeout=_VISION_TIMEOUT_SEC)
            resp.raise_for_status()
            capabilities = resp.json().get("capabilities") or []
        except Exception as exc:
            debug_log(f"model capabilities unavailable ({type(exc).__name__})", "llm")
            return False
        _VISION[key] = "vision" in capabilities
        debug_log(f"model vision capability: {_VISION[key]}", "llm")
        return _VISION[key]

    def list_models(self, timeout_sec: float = 5.0) -> List[str]:
        """List installed Ollama models via ``GET /api/tags``."""
        try:
            resp = requests.get(f"{self._base_url}/api/tags", timeout=timeout_sec)
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models", []) if isinstance(data, dict) else []
            names: List[str] = []
            for m in models:
                if isinstance(m, dict):
                    name = m.get("name")
                    if isinstance(name, str) and name:
                        names.append(name)
            return names
        except Exception:
            return []

    def release(self, model: str, timeout_sec: float = 5.0) -> bool:
        """Unload ``model`` with a ``keep_alive: 0`` request, only when ``GET /api/ps`` lists it.

        Checking first matters: an unload request for a model that is not
        resident would make Ollama load it before dropping it.
        """
        if not self._base_url or not model:
            return False
        wanted = {model, model if ":" in model else f"{model}:latest"}
        try:
            resp = requests.get(f"{self._base_url}/api/ps", timeout=timeout_sec)
            resp.raise_for_status()
            data = resp.json()
            running = data.get("models", []) if isinstance(data, dict) else []
            names = {m.get("name") for m in running if isinstance(m, dict)}
            names |= {m.get("model") for m in running if isinstance(m, dict)}
            if not wanted & names:
                return False
            resp = requests.post(f"{self._base_url}/api/chat",
                                 json={"model": model, "messages": [], "keep_alive": 0},
                                 timeout=timeout_sec)
            return resp.status_code == 200
        except Exception as exc:
            debug_log(f"Ollama release failed: {type(exc).__name__}", "llm")
            return False

    def warm_up(
        self,
        model: str,
        timeout_sec: float = 60.0,
    ) -> bool:
        """Verify Ollama and exercise inference with the live request's load shape.

        The backend owns context size, thinking default and residency. A
        one-token completion allocates the inference pipeline within the
        caller's total probe budget. Failures return False.
        """
        if not self._base_url or not model:
            return False
        try:
            # Verify the server is actually Ollama before warming up —
            # a non-Ollama HTTP server on the same port could return 200
            # to a chat POST and produce a false positive.
            started = time.monotonic()
            version_to = min(timeout_sec, 5.0)
            ok, _ = check_version(self._base_url, timeout=version_to)
            if not ok:
                return False

            remaining = timeout_sec - (time.monotonic() - started)
            if remaining <= 0:
                return False
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "ping"},
                ],
                "stream": False,
                "cache_prompt": True,
                "options": {"num_predict": 1, "temperature": 0.0},
            }
            self._apply_request_shape(payload)
            debug_log(f"Ollama warmup shape: num_ctx={self._num_ctx}, keep_alive={self._keep_alive}", "llm")
            resp = requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=remaining,
            )
            return resp.status_code == 200
        except Exception:
            return False
