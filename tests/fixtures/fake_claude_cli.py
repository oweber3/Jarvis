"""A stand-in for ``claude -p --input-format stream-json --output-format stream-json``.

Replays the frame shapes recorded from Claude Code 2.1.288 (sanitised): the in-process MCP handshake
over control requests, ``system/init``, assistant ``tool_use`` frames, ``tools/call`` control
requests answered by the host, ``StructuredOutput`` and the ``result`` frame.

Usage: fake_claude_cli.py MODE [claude arguments...]

``auth status --json`` prints the sign-in described by ``FAKE_CLAUDE_AUTH`` (default ``claude.ai``).
``FAKE_CLAUDE_RECORD`` names a file that receives the arguments and selected environment variables.
"""
import json
import os
import sys
import threading
import time
import uuid

MODE = sys.argv[1]
ARGS = sys.argv[2:]
MODELS = [
    {"value": "default", "displayName": "Default (recommended)", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "sonnet", "displayName": "Sonnet", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "haiku", "displayName": "Haiku", "supportsEffort": None, "supportedEffortLevels": None},
]
ENV_KEYS = ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
            "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CONFIG_DIR")

_out_lock = threading.Lock()
_pending = {}
_lines = []
_lines_ready = threading.Condition()


def emit(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def arg(name, default=None):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


def record():
    path = os.environ.get("FAKE_CLAUDE_RECORD")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"args": ARGS, "env": {k: os.environ.get(k) for k in ENV_KEYS}, "cwd": os.getcwd()}, fh)


def host_request(subtype_payload, timeout=10.0):
    """Send a control request to the host and wait for its control_response."""
    request_id = str(uuid.uuid4())
    done = threading.Event()
    _pending[request_id] = {"done": done}
    emit({"type": "control_request", "request_id": request_id, "request": subtype_payload})
    if not done.wait(timeout):
        return None
    return _pending.pop(request_id)["response"]


def mcp(method, params=None, msg_id=None):
    message = {"method": method, "jsonrpc": "2.0"}
    if params is not None:
        message["params"] = params
    if msg_id is not None:
        message["id"] = msg_id
    response = host_request({"subtype": "mcp_message", "server_name": "jarvis", "message": message})
    return ((response or {}).get("response") or {}).get("mcp_response")


STATE = {"tools": None, "interrupted": threading.Event(), "next_id": 2}


def handshake():
    mcp("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "claude-code", "version": "2.1.288"}}, 0)


def after_init():
    mcp("notifications/initialized")
    listed = mcp("tools/list", None, 1)
    STATE["tools"] = ((listed or {}).get("result") or {}).get("tools")


def assistant(content):
    emit({"type": "assistant", "message": {"model": "claude-haiku-4-5-20251001", "id": f"msg_{uuid.uuid4().hex[:12]}",
                                           "type": "message", "role": "assistant", "content": content,
                                           "stop_reason": None},
          "parent_tool_use_id": None, "session_id": arg("--session-id"), "uuid": str(uuid.uuid4())})


def call_tool(request_id, tool_name, arguments, name="jarvis_execute"):
    use_id = f"toolu_{uuid.uuid4().hex[:12]}"
    envelope = {"request_id": request_id, "tool_name": tool_name, "arguments": arguments}
    assistant([{"type": "tool_use", "id": use_id, "name": f"mcp__jarvis__{name}", "input": envelope}])
    msg_id = STATE["next_id"]
    STATE["next_id"] += 1
    response = mcp("tools/call", {"name": name, "arguments": envelope,
                                  "_meta": {"claudecode/toolUseId": use_id, "progressToken": msg_id}}, msg_id)
    content = ((response or {}).get("result") or {}).get("content") or [{"type": "text", "text": ""}]
    emit({"type": "user", "message": {"role": "user", "content": [{"tool_use_id": use_id, "type": "tool_result",
                                                                    "content": content}]},
          "session_id": arg("--session-id")})
    return json.loads(content[0]["text"]) if content[0].get("text") else None


def result(answer=None, *, subtype="success", is_error=False, api_status=None, terminal="completed", text=None):
    frame = {"type": "result", "subtype": subtype, "is_error": is_error, "api_error_status": api_status,
             "duration_ms": 1200, "num_turns": 2, "session_id": arg("--session-id"),
             "stop_reason": "tool_use", "terminal_reason": terminal, "permission_denials": [],
             "result": text if text is not None else (json.dumps(answer) if answer else "")}
    if answer is not None:
        frame["structured_output"] = answer
        assistant([{"type": "tool_use", "id": "toolu_out", "name": "StructuredOutput", "input": answer}])
    emit(frame)


def run_turn(text):
    emit({"type": "system", "subtype": "init", "session_id": arg("--session-id"), "model": arg("--model"),
          "tools": ["StructuredOutput", "mcp__jarvis__jarvis_execute"], "permissionMode": arg("--permission-mode"),
          "apiKeySource": "none", "mcp_servers": [{"name": "jarvis", "status": "connected", "source": "sdk"}],
          "plugins": [], "skills": [], "slash_commands": []})
    request = json.loads(text[text.index("{"):]) if "{" in text else {}
    rid = request.get("request_id", "")
    if MODE == "answer":
        result({"status": "completed", "reply": f"You said: {request.get('utterance')}"})
    elif MODE == "tool":
        data = call_tool(rid, "getTime", {})
        result({"status": "completed", "reply": f"Tool said: {(data or {}).get('text')}"})
    elif MODE == "two_tools":
        call_tool(rid, "getTime", {})
        call_tool(rid, "getTime", {})
        result({"status": "completed", "reply": "Twice."})
    elif MODE == "wrong_tool":
        data = call_tool(rid, "getTime", {}, name="something_else")
        result({"status": "completed", "reply": json.dumps(data)})
    elif MODE == "rate_limited":
        result(subtype="error_during_execution", is_error=True, api_status=429, text="Rate limited")
    elif MODE == "max_turns":
        result(subtype="error_max_turns", is_error=True, terminal="max_turns", text="")
    elif MODE == "no_answer":
        result(text="Just some prose.")
    elif MODE == "die_mid_turn":
        os._exit(3)
    elif MODE == "wait_interrupt":
        STATE["interrupted"].wait(20)
        result(subtype="error_during_execution", is_error=True, terminal="aborted_streaming", text="")
    elif MODE == "can_use_tool":
        response = host_request({"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "dir"}})
        result({"status": "completed", "reply": json.dumps((response or {}).get("response"))})
    elif MODE == "turn_activity_unprompted":
        pass


def reader():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        if msg.get("type") == "control_response":
            response = msg.get("response") or {}
            slot = _pending.get(response.get("request_id"))
            if slot is not None:
                slot["response"] = response
                slot["done"].set()
            continue
        with _lines_ready:
            _lines.append(msg)
            _lines_ready.notify_all()
    with _lines_ready:
        _lines.append(None)
        _lines_ready.notify_all()


def next_line():
    with _lines_ready:
        while not _lines:
            _lines_ready.wait()
        return _lines.pop(0)


def main():
    if ARGS[:2] == ["auth", "status"]:
        method = os.environ.get("FAKE_CLAUDE_AUTH", "claude.ai")
        print(json.dumps({"loggedIn": method != "none", "authMethod": method, "apiProvider": "firstParty",
                          "subscriptionType": "max" if method == "claude.ai" else None,
                          "configDirectory": "x"}))
        return
    record()
    if MODE == "exit_now":
        sys.exit(2)
    threading.Thread(target=reader, daemon=True).start()
    if MODE == "oversized":
        emit({"type": "system", "subtype": "padding", "data": "x" * (2 * 1024 * 1024)})
    if MODE == "malformed":
        with _out_lock:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
    while True:
        msg = next_line()
        if msg is None:
            return
        if msg.get("type") == "control_request":
            subtype = (msg.get("request") or {}).get("subtype")
            if subtype == "initialize":
                if MODE == "no_init":
                    continue
                handshake()
                emit({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"],
                      "response": {"commands": [], "models": MODELS, "account": {"tokenSource": "none"},
                                   "pid": os.getpid()}}})
                threading.Thread(target=after_init, daemon=True).start()
                if MODE == "turn_activity_unprompted":
                    threading.Thread(target=lambda: (time.sleep(0.3), run_turn("{}"),
                                                     assistant([{"type": "text", "text": "Injected"}])),
                                     daemon=True).start()
            elif subtype == "interrupt":
                STATE["interrupted"].set()
                emit({"type": "control_response", "response": {"subtype": "success",
                                                               "request_id": msg["request_id"], "response": {}}})
            else:
                emit({"type": "control_response", "response": {"subtype": "error", "request_id": msg["request_id"],
                                                               "error": "unsupported"}})
        elif msg.get("type") == "user":
            text = msg["message"]["content"][0]["text"]
            threading.Thread(target=run_turn, args=(text,), daemon=True).start()


if __name__ == "__main__":
    main()
