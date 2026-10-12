"""A loopback HTTP server that serves one calendar, for agenda tests. Never contacts a real service.

``body`` is what it serves; ``status`` its HTTP status; ``requests`` counts what was asked.
``stop()`` shuts it down so the address stops answering (an offline PC).
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeIcsServer:
    def __init__(self, body: str = "", status: int = 200, content_type: str = "text/calendar"):
        self.body = body
        self.status = status
        self.content_type = content_type
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                owner.requests += 1
                data = owner.body.encode("utf-8")
                self.send_response(owner.status)
                self.send_header("Content-Type", owner.content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/private-abc123/basic.ics"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
