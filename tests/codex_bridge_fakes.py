"""In-memory stand-ins for the Codex app-server and the model behind it.

``FakeAppServer`` implements the ``AppServerClient`` surface the bridge service uses. Each
``turn/start`` runs a script on its own thread (like Codex does), driving a ``Model`` that reads the
request from the turn, sends ``item/tool/call`` requests, its final ``agentMessage`` and the terminal
``turn/completed`` notification.
"""
from __future__ import annotations

import itertools
import json
import queue
import re
import threading
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from jarvis.codex_bridge.app_server import AppServerError
from jarvis.tools.types import ToolExecutionResult

LUNA = {"id": "gpt-6-luna", "model": "gpt-6-luna",
        "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high")]}
NO_RESPONSE = object()


class Model:
    """One turn's view of the conversation, driven from a script thread."""

    def __init__(self, server: "FakeAppServer", thread_id: str, turn_id: str, params: Dict[str, Any]):
        self.server, self.thread_id, self.turn_id = server, thread_id, turn_id
        self.text = params["input"][0]["text"]
        match = re.search(r"\{.*\}", self.text, re.DOTALL)
        self.request: Dict[str, Any] = json.loads(match.group()) if match else {}
        self.request_id = self.request.get("request_id", "")
        self.context: Dict[str, Any] = params.get("additionalContext") or {}
        self.output_schema = params.get("outputSchema")
        self.finished = threading.Event()
        self.results: List[Any] = []
        # Inputs added to the running turn with ``turn/steer``.
        self.steered: List[List[Dict[str, Any]]] = []

    def call(self, tool: str, arguments: Any, call_id: Optional[str] = None, thread: Optional[str] = None,
             turn: Optional[str] = None, timeout: float = 5.0) -> Any:
        """Send a dynamic tool call; returns {"success", "data"} or NO_RESPONSE."""
        params = {"threadId": thread or self.thread_id, "turnId": turn or self.turn_id,
                  "callId": call_id or f"call-{next(self.server.ids)}", "tool": tool, "arguments": arguments}
        response = self.server.server_request("item/tool/call", params, timeout)
        if response is NO_RESPONSE or "error" in response:
            out = response if response is NO_RESPONSE else {"error": response["error"]}
        else:
            result = response["result"]
            out = {"success": result["success"], "data": json.loads(result["contentItems"][0]["text"])}
            if len(result["contentItems"]) > 1:
                out["extra_items"] = result["contentItems"][1:]
        self.results.append(out)
        return out

    def execute(self, tool: str, args: Any, **kw) -> Any:
        return self.call("jarvis_execute", {"request_id": self.request_id, "tool_name": tool, "arguments": args}, **kw)

    def say(self, text: str, phase: Optional[str] = "final_answer") -> None:
        """An assistant message item, as Codex reports it when the item completes."""
        self.server.notify("item/completed", {"threadId": self.thread_id, "turnId": self.turn_id, "item": {
            "type": "agentMessage", "id": f"msg-{next(self.server.ids)}", "text": text, "phase": phase}})

    def answer(self, status: str, reply: str) -> None:
        """The final message in the turn's output schema."""
        self.say(json.dumps({"status": status, "reply": reply}))

    def finish(self, status: str = "completed", error: Optional[Dict[str, Any]] = None) -> None:
        if self.finished.is_set():
            return
        self.finished.set()
        self.server.notify("turn/completed", {"threadId": self.thread_id,
                                              "turn": {"id": self.turn_id, "status": status, "error": error}})


class FakeAppServer:
    def __init__(self, script: Optional[Callable[[Model], None]] = None, *, account: Any = "chatgpt",
                 requires_auth: bool = True, models: Optional[List[Dict[str, Any]]] = None,
                 start_error: Optional[str] = None, effective_config: Optional[Dict[str, Any]] = None,
                 fail: Optional[Dict[str, str]] = None):
        self.script = script
        self.account = {"type": account} if isinstance(account, str) else account
        self.requires_auth = requires_auth
        self.models = [LUNA] if models is None else models
        self.start_error = start_error
        self.effective_config = effective_config if effective_config is not None else {
            "mcp_servers": {"personal": {}}, "plugins": {"p@m": {"enabled": True}}}
        self.fail = dict(fail or {})
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.requests: List[tuple] = []
        self.generation = 0
        self.starts = 0
        self.closes = 0
        self.turns: List[Model] = []
        self.threads: Dict[str, Dict[str, Any]] = {}
        self.unsubscribed: List[str] = []
        self.answers: Dict[Any, Dict[str, Any]] = {}
        self.ids = itertools.count(1)
        self._waiters: Dict[Any, threading.Event] = {}
        self._alive = False
        self._lock = threading.Lock()

    # -- AppServerClient surface ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._alive

    def start(self) -> Dict[str, Any]:
        if self._alive:
            return {}
        if self.start_error:
            raise AppServerError(self.start_error)
        self.generation += 1
        self.starts += 1
        self._alive = True
        return {"userAgent": "fake"}

    def request(self, method: str, params: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        if not self._alive:
            raise AppServerError("closed")
        self.requests.append((method, params))
        if method in self.fail:
            raise AppServerError("rpc_error", self.fail[method], -32600)
        if method == "account/read":
            return {"requiresOpenaiAuth": self.requires_auth, "account": self.account}
        if method == "model/list":
            return {"data": list(self.models)}
        if method == "config/read":
            return {"config": self.effective_config}
        if method == "thread/start":
            servers = set((self.effective_config.get("mcp_servers") or {}))
            for key in (params.get("config") or {}):
                if key.startswith("mcp_servers.") and key.split(".")[1] not in servers:
                    raise AppServerError("rpc_error", "invalid configuration: missing field `command`", -32600)
            thread_id = f"thread-{next(self.ids)}"
            self.threads[thread_id] = params
            return {"thread": {"id": thread_id, "ephemeral": True, "path": None},
                    "instructionSources": []}
        if method == "turn/start":
            model = Model(self, params["threadId"], f"turn-{next(self.ids)}", params)
            self.turns.append(model)
            if self.script is not None:
                threading.Thread(target=self._run_script, args=(model,), daemon=True).start()
            return {"turn": {"id": model.turn_id, "status": "inProgress"}}
        if method == "turn/steer":
            model = next((m for m in self.turns if m.turn_id == params["expectedTurnId"]), None)
            if model is None or model.finished.is_set():
                raise AppServerError("rpc_error", "no active turn", -32600)
            model.steered.append(params["input"])
            return {"turnId": model.turn_id}
        if method == "turn/interrupt":
            for model in self.turns:
                if model.turn_id == params["turnId"]:
                    model.finish("interrupted")
            return {}
        if method == "thread/unsubscribe":
            self.unsubscribed.append(params["threadId"])
            return {"status": "unsubscribed"}
        raise AppServerError("rpc_error", "unknown method", -32601)

    def respond(self, rpc_id: Any, result: Dict[str, Any]) -> None:
        self._answer(rpc_id, {"result": result})

    def respond_error(self, rpc_id: Any, code: int, message: str) -> None:
        self._answer(rpc_id, {"error": {"code": code, "message": message}})

    def poll_event(self, timeout_sec: float) -> Optional[Dict[str, Any]]:
        try:
            return self.events.get(timeout=max(0.0, timeout_sec))
        except queue.Empty:
            return None

    def close(self, timeout_sec: float = 5.0) -> None:
        self.closes += 1
        self._alive = False

    # -- test controls ------------------------------------------------------------------------

    def methods(self) -> List[str]:
        return [m for m, _ in self.requests]

    def params_of(self, method: str) -> List[Dict[str, Any]]:
        return [p for m, p in self.requests if m == method]

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self.events.put({"generation": self.generation, "kind": "notification", "method": method,
                         "params": params, "id": None})

    def server_request(self, method: str, params: Dict[str, Any], timeout: float = 5.0) -> Any:
        rpc_id = f"srv-{next(self.ids)}"
        done = threading.Event()
        with self._lock:
            self._waiters[rpc_id] = done
        self.events.put({"generation": self.generation, "kind": "request", "method": method,
                         "params": params, "id": rpc_id})
        if not done.wait(timeout):
            return NO_RESPONSE
        return self.answers[rpc_id]

    def die(self) -> None:
        """The child exits unexpectedly."""
        self._alive = False
        self.events.put({"generation": self.generation, "kind": "closed", "reason": "exited"})

    def _answer(self, rpc_id: Any, message: Dict[str, Any]) -> None:
        with self._lock:
            self.answers[rpc_id] = message
            done = self._waiters.pop(rpc_id, None)
        if done is not None:
            done.set()

    def _run_script(self, model: Model) -> None:
        self.script(model)


def scripted(*steps):
    """Script steps: ('exec', tool, args), ('answer', status, reply), ('say', text[, phase]),
    ('finish', status, error).

    A script without an explicit finish ends with a successful ``turn/completed``."""

    def run(model: Model) -> None:
        for step in steps:
            if step[0] == "exec":
                model.execute(step[1], step[2])
            elif step[0] == "answer":
                model.answer(step[1], step[2])
            elif step[0] == "say":
                model.say(step[1], *step[2:])
            elif step[0] == "finish":
                model.finish(step[1], step[2] if len(step) > 2 else None)
        model.finish()

    return run


class FakeStore:
    def __init__(self):
        self.observers = []
        self.pending = {}

    def add_observer(self, cb):
        self.observers.append(cb)

    def remove_observer(self, cb):
        if cb in self.observers:
            self.observers.remove(cb)

    def pending_for_ref(self, ref):
        return self.pending.get(ref)

    def discard_pending(self, request_id, reason="cancelled"):
        self.pending = {ref: p for ref, p in self.pending.items() if p.request.id != request_id}

    def emit(self, confirmation_id, outcome):
        for cb in list(self.observers):
            cb(confirmation_id, outcome)


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.threads = []
        self.next = None
        self.raise_next = False

    def __call__(self, db, cfg, tool_name, tool_args, system_prompt, original_prompt, redacted_text,
                 max_retries=1, language=None, quiet=False, request_ref=None, allowed_tools=None):
        self.calls.append((tool_name, tool_args, request_ref, quiet))
        self.threads.append(threading.get_ident())
        if self.raise_next:
            raise RuntimeError("tool exploded")
        if self.next is not None:
            result, self.next = self.next, None
            return result
        return ToolExecutionResult(success=True, reply_text=f"{tool_name} ok")


def make_cfg(**kw):
    base = dict(codex_model="gpt-6-luna", codex_reasoning_effort="low", codex_timeout_sec=5.0,
                codex_queue_limit=1, codex_max_tool_calls=8, codex_share_long_term_memory=False,
                llm_tools_timeout_sec=5.0)
    base.update(kw)
    return SimpleNamespace(**base)
