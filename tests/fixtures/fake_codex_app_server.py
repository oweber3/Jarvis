"""Scripted stand-in for ``codex app-server --listen stdio://`` used by the client tests.

Usage: ``python fake_codex_app_server.py <mode> [original arguments...]``. It speaks the
newline-delimited JSON-RPC shape of the real server. Test-only methods start with ``test/``.
"""
import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
ARGS = sys.argv[2:]
out = sys.stdout.buffer
received = []


def write(raw: bytes) -> None:
    out.write(raw)
    out.flush()


def send(obj) -> None:
    write((json.dumps(obj) + "\n").encode("utf-8"))


def handle(msg) -> None:
    method = msg.get("method")
    rid = msg.get("id")
    if method is None:
        # A response to one of our server requests: report it back as a notification.
        send({"method": "test/responded", "params": {"id": rid, "result": msg.get("result"),
                                                     "error": msg.get("error")}})
        return
    received.append(method)
    if rid is None:
        return
    if method == "initialize":
        if MODE == "no_init":
            return
        send({"id": rid, "result": {"userAgent": "fake/0.0", "codexHome": "x", "platformFamily": "test",
                                    "platformOs": "test", "params": msg.get("params")}})
    elif method == "test/order":
        send({"id": rid, "result": {"received": list(received)}})
    elif method == "test/args":
        send({"id": rid, "result": {"argv": ARGS}})
    elif method == "test/events":
        # Interleave a notification and a server request before the response; fragment one
        # message across writes and coalesce two into one write.
        note = json.dumps({"method": "test/note", "params": {"n": 1}}).encode("utf-8") + b"\n"
        write(note[:7])
        time.sleep(0.05)
        write(note[7:])
        req = json.dumps({"id": "srv-1", "method": "item/tool/call", "params": {"tool": "t"}}).encode("utf-8")
        resp = json.dumps({"id": rid, "result": {"ok": True}}).encode("utf-8")
        write(req + b"\n" + resp + b"\n")
    elif method == "test/error":
        send({"id": rid, "error": {"code": -32600, "message": "bad request " + "x" * 5000}})
    elif method == "test/slow":
        pass
    elif method == "test/big":
        write(b'{"method": "test/big", "params": {"data": "' + b"a" * (2 * 1024 * 1024) + b'"}}\n')
    elif method == "test/bad":
        write(b"this is not json\n")
    elif method == "test/exit":
        sys.exit(3)
    else:
        send({"id": rid, "error": {"code": -32601, "message": "unknown method"}})


def main() -> None:
    if MODE == "stderr_flood":
        chunk = ("noise " * 100 + "\n").encode("utf-8")
        for _ in range(4000):
            sys.stderr.buffer.write(chunk)
        sys.stderr.buffer.flush()
    for line in sys.stdin.buffer:
        line = line.strip()
        if line:
            handle(json.loads(line))
    if MODE == "ignore_eof":
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
