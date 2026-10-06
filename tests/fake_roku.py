"""A fake Roku External Control Protocol (ECP) device for tests.

Binds to loopback on a free port and records every request, so tests assert on what the TV was asked
to do. Nothing here ever contacts a real device. ``mode`` switches the behaviours the real TV shows:

- ``ok``: everything answers.
- ``limited``: queries answer, keypress and launch return 403 (Settings > System > Advanced system
  settings > Control by mobile apps set to Limited or Disabled).
- ``standby``: device-info reports a standby power mode, otherwise as ``ok``.
"""
from __future__ import annotations

import threading
from xml.sax.saxutils import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

DEVICE_INFO = """<?xml version="1.0" encoding="UTF-8" ?>
<device-info>
  <udn>fake-udn-1234</udn>
  <serial-number>FAKESERIAL1</serial-number>
  <device-id>FAKEDEVICEID1</device-id>
  <user-device-name>Living Room TV</user-device-name>
  <model-name>Fake Roku TV</model-name>
  <power-mode>{power}</power-mode>
  <supports-ecs-textedit>true</supports-ecs-textedit>
</device-info>"""

DEFAULT_APPS = [
    ("tvin.hdmi1", "tvin", "HDMI 1"),
    ("tvin.tuner", "tvin", "Live TV"),
    ("12", "appl", "Netflix"),
    ("13", "appl", "Prime Video"),
    ("2285", "appl", "Hulu"),
    ("291097", "appl", "Disney Plus"),
    ("61322", "appl", "HBO Max"),
    ("551012", "appl", "Apple TV"),
    ("837", "appl", "YouTube"),
    ("100", "appl", "Plex"),
    ("101", "appl", "Plex Media Server"),
]


class FakeRoku:
    def __init__(self, apps=None, mode="ok", active="Roku"):
        self.apps = list(DEFAULT_APPS if apps is None else apps)
        self.mode = mode
        self.active = active
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _record(self):
                with outer._lock:
                    outer.requests.append((self.command, self.path))

            def _send(self, code, body=b"", ctype="text/xml"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._record()
                if self.path == "/query/device-info":
                    power = "Ready" if outer.mode == "standby" else "PowerOn"
                    self._send(200, DEVICE_INFO.format(power=power).encode())
                elif self.path == "/query/apps":
                    items = "".join(f'<app id="{i}" type="{t}" version="1.0">{escape(n)}</app>' for i, t, n in outer.apps)
                    self._send(200, f"<apps>{items}</apps>".encode())
                elif self.path == "/query/active-app":
                    self._send(200, f"<active-app><app>{outer.active}</app></active-app>".encode())
                else:
                    self._send(404)

            def do_POST(self):
                self._record()
                if outer.mode == "limited":
                    self._send(403, b"Forbidden")
                    return
                if self.path.startswith("/keypress/") or self.path.startswith("/launch/"):
                    self._send(200)
                else:
                    self._send(404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def posts(self) -> list[str]:
        """Decoded paths of every POST, in order."""
        with self._lock:
            return [unquote(path) for method, path in self.requests if method == "POST"]

    def keys(self) -> list[str]:
        """Names of the keys pressed, in order (``Lit_x`` shown as written)."""
        return [path.split("/keypress/", 1)[1] for path in self.posts() if path.startswith("/keypress/")]
