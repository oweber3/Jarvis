"""In-memory stand-ins for headless Claude Code sessions and the model behind them.

``FakeClaude`` is the session factory the bridge service uses. Each ``FakeSession`` behaves like one
``claude -p`` process: ``initialize`` runs the in-process MCP handshake (the host answers
``initialize`` and ``tools/list``), and each user message runs a script on its own thread that drives a
``Model``: tool calls become ``tools/call`` control requests the host answers, and the turn ends with a
``result`` frame, as recorded from Claude Code 2.1.288.
"""
from __future__ import annotations

import itertools
import json
import queue
import re
import threading
from typing import Any, Callable, Dict, List, Optional

from jarvis.claude_bridge.cli import ClaudeCliError

MODELS = [
    {"value": "default", "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "sonnet", "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "haiku", "supportsEffort": None, "supportedEffortLevels": None},
]
NO_RESPONSE = object()
SIGNED_IN = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}


class Model:
    """One turn, driven from a script thread."""

    def __init__(self, session: "FakeSession", text: str):
        self.session = session
        self.text = text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        self.request: Dict[str, Any] = json.loads(match.group()) if match else {}
        self.request_id = self.request.get("request_id", "")
        self.results: List[Any] = []
        self.finished = threading.Event()

    def call(self, envelope: Any, name: str = "jarvis_execute", msg_id: Optional[int] = None,
             timeout: float = 5.0) -> Any:
        """A ``tools/call``; returns {"is_error", "data"} or NO_RESPONSE."""
        msg_id = msg_id if msg_id is not None else next(self.session.ids)
        response = self.session.host_request({"subtype": "mcp_message", "server_name": "jarvis", "message": {
            "method": "tools/call", "jsonrpc": "2.0", "id": msg_id,
            "params": {"name": name, "arguments": envelope}}}, timeout)
        if response is NO_RESPONSE:
            out = NO_RESPONSE
        else:
            mcp = (response.get("response") or {}).get("mcp_response") or {}
            if "error" in mcp:
                out = {"error": mcp["error"]}
            else:
                result = mcp["result"]
                out = {"is_error": result.get("isError"), "data": json.loads(result["content"][0]["text"])}
                images = [block for block in result["content"][1:] if block.get("type") == "image"]
                if images:
                    out["images"] = [(block["mimeType"], block["data"]) for block in images]
        self.results.append(out)
        return out

    def execute(self, tool: str, args: Any, **kw) -> Any:
        return self.call({"request_id": self.request_id, "tool_name": tool, "arguments": args}, **kw)

    def answer(self, status: str, reply: str) -> None:
        self.result(structured={"status": status, "reply": reply})

    def result(self, structured: Any = None, *, text: Optional[str] = None, subtype: str = "success",
               is_error: bool = False, api_status: Optional[int] = None, terminal: str = "completed") -> None:
        if self.finished.is_set():
            return
        self.finished.set()
        frame = {"type": "result", "subtype": subtype, "is_error": is_error, "api_error_status": api_status,
                 "terminal_reason": terminal, "session_id": self.session.session_id,
                 "result": text if text is not None else (json.dumps(structured) if structured else "")}
        if structured is not None:
            frame["structured_output"] = structured
        self.session.message(frame)


class FakeSession:
    def __init__(self, factory: "FakeClaude", session_id: str, args: List[str]):
        self.factory = factory
        self.session_id = session_id
        self.args = list(args)
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.alive = False
        self.closes = 0
        self.controls: List[str] = []
        self.user_texts: List[str] = []
        self.turns: List[Model] = []
        self.tools: Optional[List[Dict[str, Any]]] = None
        self.ids = itertools.count(2)
        self.answers: Dict[str, Dict[str, Any]] = {}
        self._waiters: Dict[str, threading.Event] = {}
        self._req_ids = itertools.count(1)
        self._lock = threading.Lock()

    # -- ClaudeSession surface ----------------------------------------------------------

    @property
    def pid(self) -> int:
        return 4242

    def arg(self, name: str) -> Optional[str]:
        return self.args[self.args.index(name) + 1] if name in self.args else None

    def start(self) -> None:
        if self.factory.start_error:
            raise ClaudeCliError(self.factory.start_error)
        self.alive = True
        self.factory.started.append(self)

    def send_control(self, subtype: str, payload: Optional[Dict[str, Any]] = None) -> str:
        if not self.alive:
            raise ClaudeCliError("closed")
        request_id = f"ctl-{next(self._req_ids)}"
        self.controls.append(subtype)
        if subtype == "initialize":
            threading.Thread(target=self._initialise, args=(request_id,), daemon=True).start()
        elif subtype == "interrupt":
            for model in self.turns:
                model.result(subtype="error_during_execution", is_error=True, terminal="aborted_streaming")
            self.events.put({"kind": "control_response", "request_id": request_id,
                             "response": {"subtype": "success", "request_id": request_id, "response": {}}})
        return request_id

    def send_user_text(self, text: str) -> None:
        if not self.alive:
            raise ClaudeCliError("closed")
        self.user_texts.append(text)
        model = Model(self, text)
        self.turns.append(model)
        if self.factory.script is not None:
            threading.Thread(target=self._run, args=(model,), daemon=True).start()

    def respond(self, request_id: Any, response: Dict[str, Any]) -> None:
        self._answer(request_id, {"subtype": "success", "response": response})

    def respond_error(self, request_id: Any, message: str) -> None:
        self._answer(request_id, {"subtype": "error", "error": message})

    def poll_event(self, timeout_sec: float) -> Optional[Dict[str, Any]]:
        try:
            return self.events.get(timeout=max(0.0, timeout_sec))
        except queue.Empty:
            return None

    def close(self, timeout_sec: float = 3.0) -> None:
        self.closes += 1
        self.alive = False

    # -- test controls --------------------------------------------------------------------

    def message(self, frame: Dict[str, Any]) -> None:
        self.events.put({"kind": "message", "type": frame.get("type"), "message": frame})

    def host_request(self, request: Dict[str, Any], timeout: float = 5.0) -> Any:
        request_id = f"cli-{next(self._req_ids)}"
        done = threading.Event()
        with self._lock:
            self._waiters[request_id] = done
        self.events.put({"kind": "control_request", "request_id": request_id, "request": request})
        if not done.wait(timeout):
            return NO_RESPONSE
        return self.answers[request_id]

    def mcp(self, method: str, params: Any = None, msg_id: Optional[int] = None, timeout: float = 5.0) -> Any:
        message: Dict[str, Any] = {"method": method, "jsonrpc": "2.0"}
        if params is not None:
            message["params"] = params
        if msg_id is not None:
            message["id"] = msg_id
        response = self.host_request({"subtype": "mcp_message", "server_name": "jarvis", "message": message},
                                     timeout)
        return NO_RESPONSE if response is NO_RESPONSE else (response.get("response") or {}).get("mcp_response")

    def die(self) -> None:
        self.alive = False
        self.events.put({"kind": "closed", "reason": "exited"})

    def _answer(self, request_id: Any, response: Dict[str, Any]) -> None:
        with self._lock:
            self.answers[request_id] = response
            done = self._waiters.pop(request_id, None)
        if done is not None:
            done.set()

    def _initialise(self, request_id: str) -> None:
        self.mcp("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}, 0)
        if self.factory.no_init:
            return
        self.events.put({"kind": "control_response", "request_id": request_id, "response": {
            "subtype": "success", "request_id": request_id,
            "response": {"models": self.factory.models, "commands": [], "account": {"tokenSource": "none"}}}})
        self.mcp("notifications/initialized")
        listed = self.mcp("tools/list", None, 1)
        self.tools = (listed or {}).get("result", {}).get("tools") if listed is not NO_RESPONSE else None

    def _run(self, model: Model) -> None:
        self.message({"type": "system", "subtype": "init", "session_id": self.session_id,
                      "tools": ["StructuredOutput", "mcp__jarvis__jarvis_execute"], "apiKeySource": "none"})
        self.factory.script(model)
        if not model.finished.is_set():
            model.answer("completed", "Done.")


class FakeClaude:
    """Session factory: ``factory(session_id, args) -> FakeSession``."""

    def __init__(self, script: Optional[Callable[[Model], None]] = None, *, models: Optional[List[Dict]] = None,
                 start_error: Optional[str] = None, no_init: bool = False):
        self.script = script
        self.models = MODELS if models is None else models
        self.start_error = start_error
        self.no_init = no_init
        self.sessions: List[FakeSession] = []
        self.started: List[FakeSession] = []

    def __call__(self, session_id: str, args: List[str]) -> FakeSession:
        session = FakeSession(self, session_id, args)
        self.sessions.append(session)
        return session

    def used(self) -> List[FakeSession]:
        """Sessions that carried a request."""
        return [s for s in self.sessions if s.user_texts]


def scripted(*steps):
    """Script steps: ('exec', tool, args), ('answer', status, reply), ('result', kwargs)."""

    def run(model: Model) -> None:
        for step in steps:
            if step[0] == "exec":
                model.execute(step[1], step[2])
            elif step[0] == "answer":
                model.answer(step[1], step[2])
            elif step[0] == "result":
                model.result(**step[1])

    return run


def make_cfg(**kw):
    from types import SimpleNamespace
    base = dict(claude_model="sonnet", claude_effort="low", claude_executable="claude", claude_timeout_sec=5.0,
                claude_queue_limit=1, claude_max_tool_calls=8, claude_share_long_term_memory=False,
                llm_tools_timeout_sec=5.0)
    base.update(kw)
    return SimpleNamespace(**base)
