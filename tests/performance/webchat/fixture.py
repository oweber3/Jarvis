"""Serves the web chat (or the phone app) over a seeded chat store and the test suite's fake daemon, for measuring
and screenshotting the page. A development tool, run by hand; nothing here is shipped or collected by pytest.

    python tests/performance/webchat/fixture.py [--static <built page>] [--port 8766] [--control-port 8799]
                                                 [--long 3000] [--phone] [--cloud] [--confirm]

The chat store holds a short chat (tool steps, a code block, a confirmation), a long chat of ``--long`` messages and
a few others in two projects. The control port takes POST /voice {"user", "reply"} (a spoken exchange landing in the
open chat), /turn {"role", "text"}, /mode {"mode"} (the fake engine: reply, hold, busy...), /release, /busy,
/state {"state"} and /step {"tool", "status", "label", "summary"} (a tool step of the request in flight). See
README.md beside this file.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]),
                    help="the checkout whose src and tests to use (default: this one)")
parser.add_argument("--static")
parser.add_argument("--port", type=int, default=8766)
parser.add_argument("--control-port", type=int, default=8799)
parser.add_argument("--long", type=int, default=3000)
parser.add_argument("--open", default="short", choices=["short", "long", "empty"])
parser.add_argument("--cloud", action="store_true")
parser.add_argument("--phone", action="store_true")
parser.add_argument("--confirm", action="store_true")
args = parser.parse_args()

repo = Path(args.repo).resolve()
sys.path[:0] = [str(repo / "src"), str(repo / "tests")]

from fake_webchat_backend import FakeWebChatBackend  # noqa: E402
from jarvis.memory.chat_store import ChatStore  # noqa: E402
from jarvis.webchat.hub import ChatHub  # noqa: E402
from jarvis.webchat.server import STATIC_DIR, ServerSettings, WebChatServer  # noqa: E402

NOW = time.time()
DAY = 86400.0

REPLY_MD = """Here is what I found for **{topic}**:

1. The first point, with a little `inline code`.
2. A second point that runs a bit longer so it wraps onto another line in a narrow window.

| Item | Value |
|------|-------|
| CPU  | 12 %  |
| RAM  | 41 %  |

```python
def total(values):
    return sum(v for v in values if v is not None)
```

Anything else?"""

SHORT = [
    ("user", "What's using the most RAM?", "typed", []),
    ("assistant", "**Chrome** is using the most memory right now: about 2.1 GB across 14 processes. "
                  "Next are *Visual Studio Code* (1.3 GB) and *Teams* (780 MB).", "typed",
     [{"tool": "systemInfo", "status": "done", "label": "Checked memory use", "summary": "14 processes"}]),
    ("user", "set the volume to 30%", "voice", []),
    ("assistant", "Volume set to 30%.", "voice",
     [{"tool": "systemVolume", "status": "done", "label": "Set the volume to 30%", "summary": None}]),
    ("user", "Can you write a quick Python function that adds up a list, skipping empty values?", "typed", []),
    ("assistant", REPLY_MD.format(topic="adding up a list"), "typed", []),
    ("user", "Delete the old screenshots folder", "typed", []),
    ("assistant", "Please confirm on your desktop.", "typed",
     [{"tool": "localFiles", "status": "waiting", "label": "Local files", "summary": None}]),
    ("assistant", "Deleted the folder.", "confirmed",
     [{"tool": "localFiles", "status": "done", "label": "Deleted a folder", "summary": None}]),
]


def seed(store: ChatStore) -> dict:
    work = store.create_project("Work")
    home = store.create_project("Home")
    ids = {}
    chat = store.create_chat(project_id=None, title="Voice and quick questions")
    ids["short"] = chat.id
    for i, (role, text, source, steps) in enumerate(SHORT):
        store.append_message(chat.id, role, text, ts=NOW - 3600 + i * 60, source=source, steps=steps)
    long_chat = store.create_chat(project_id=work.id, title="Quarterly planning notes")
    ids["long"] = long_chat.id
    start = NOW - 40 * DAY
    for i in range(args.long // 2):
        ts = start + i * (40 * DAY / max(1, args.long // 2))
        source = "voice" if i % 7 in (3, 4) else "typed"
        store.append_message(long_chat.id, "user", f"Question number {i}: what about item {i * 3}?", ts=ts,
                             source=source)
        text = REPLY_MD.format(topic=f"item {i * 3}") if i % 5 == 0 else f"Item {i * 3} is fine. Nothing to do."
        steps = [{"tool": "webSearch", "status": "done", "label": "Searched the web", "summary": "3 results"}] \
            if i % 9 == 0 else []
        store.append_message(long_chat.id, "assistant", text, ts=ts + 5, source=source, steps=steps)
    for title, project, age in (("Trip to Lisbon", home.id, 2 * DAY), ("Fix the printer", None, 0.2 * DAY),
                                ("Gift ideas", home.id, 9 * DAY), ("Code review checklist", work.id, 35 * DAY)):
        c = store.create_chat(project_id=project, title=title)
        store.append_message(c.id, "user", f"Notes about {title.lower()}", ts=NOW - age, source="typed")
        store.append_message(c.id, "assistant", "Noted.", ts=NOW - age + 3, source="typed")
    pinned = store.create_chat(project_id=None, title="Shopping list")
    store.append_message(pinned.id, "user", "Add milk and bread", ts=NOW - 5 * DAY, source="voice")
    store.append_message(pinned.id, "assistant", "Added milk and bread to your shopping list.", ts=NOW - 5 * DAY + 2,
                         source="voice")
    store.set_pinned(pinned.id, True)
    empty = store.create_chat(project_id=None)
    ids["empty"] = empty.id
    return ids


tmp = Path(tempfile.mkdtemp(prefix="webchat-fixture-"))
store = ChatStore(str(tmp / "jarvis.db"))
ids = seed(store)
store.set_active_chat_id(ids[args.open])
backend = FakeWebChatBackend()
backend.suggestion_data = {"routines": ["Morning briefing", "Focus mode"], "workspaces": ["Coding"],
                           "workspaces_available": True}
backend.reply_text = "Done. Volume set to 40%."
if args.cloud:
    backend.reply_mode = {"mode": "claude", "enabled": ["local", "claude", "codex"]}
    backend.cloud = {"mode": "claude", "model": "sonnet", "effort": "high", "ready": True}
    backend.cloud_options = [
        {"id": "sonnet", "name": "Sonnet 5.5", "is_default": True, "description": "Fast and capable",
         "efforts": [{"id": "low", "is_default": False, "description": ""},
                     {"id": "high", "is_default": True, "description": ""}]},
        {"id": "haiku", "name": "Haiku 4.5", "is_default": False, "description": "Quickest", "efforts": []},
    ]
hub = ChatHub(backend, store, sync_interval_sec=0.1)
token = None
if args.phone:
    from fake_remote_backend import FakeRemoteBackend
    from jarvis.remote.pairing import DeviceStore
    from jarvis.remote.server import STATIC_DIR as PHONE_STATIC, RemoteServer, ServerSettings as PhoneSettings
    devices = DeviceStore(tmp)
    token = devices.complete_pairing(devices.start_pairing(), "Test phone").token
    remote_backend = FakeRemoteBackend()
    if args.confirm:
        remote_backend.confirmation = {"id": "c1", "tool": "localFiles", "action": "Delete a folder",
                                       "target": "Old screenshots", "consequence": "It cannot be undone.",
                                       "expires_in": 41}
    static = Path(args.static) if args.static else PHONE_STATIC
    server = RemoteServer(hub, devices, remote_backend, PhoneSettings(
        host="127.0.0.1", port=args.port, static_dir=static,
        quick_actions=["Pause the music", "Next song", "Volume up", "What's using the most RAM?"]))
else:
    static = Path(args.static) if args.static else STATIC_DIR
    server = WebChatServer(hub, ServerSettings(host="127.0.0.1", port=args.port, static_dir=static))
server.start()


class Control(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/turn":
            backend.memory.add_message(body.get("role", "assistant"), body.get("text", "A new voice turn."))
        elif self.path == "/voice":
            backend.speak(body.get("user", "what time is it"), body.get("reply", "It's ten past four."))
        elif self.path == "/mode":
            backend.mode = body.get("mode", "reply")
        elif self.path == "/busy":
            backend.busy = bool(body.get("busy", True))
        elif self.path == "/release":
            backend.release()
        elif self.path == "/step":
            from jarvis.tools.step_log import get_step_log
            get_step_log().record(body.get("tool", "webSearch"), body.get("status", "done"), body.get("label"),
                                  body.get("summary"))
        elif self.path == "/state":
            backend.set_state(body.get("state", "idle"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(ids).encode())

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(ids).encode())


control = ThreadingHTTPServer(("127.0.0.1", args.control_port), Control)
threading.Thread(target=control.serve_forever, daemon=True).start()
ids["token"] = token
print(f"🧪 Serving the {'phone app' if args.phone else 'web chat'} fixture on port {server.port}", flush=True)
print(f"   🎛️  Control port {args.control_port}", flush=True)
print(json.dumps({"ready": True, "port": server.port, "ids": ids}), flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
