from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from typing import Any


class HealthState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._status = "booting"
        self._last_error: str | None = None

    def set_status(self, status: str, *, last_error: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._last_error = last_error

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service": "Memact AutoMod",
                "status": self._status,
                "last_error": self._last_error,
                "uptime_seconds": int(time.time() - self._started_at),
                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }


def start_health_server(state: HealthState) -> ThreadingHTTPServer | None:
    port_raw = os.getenv("PORT", "").strip()
    if not port_raw:
        return None

    try:
        port = int(port_raw)
    except ValueError:
        print(f"Skipping health server because PORT is invalid: {port_raw!r}")
        return None

    host = os.getenv("MEMACT_HTTP_HOST", "0.0.0.0").strip() or "0.0.0.0"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path not in ("/", "/healthz"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            body = json.dumps(state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="memact-health-server")
    thread.start()
    print(f"Health server listening on http://{host}:{port}")
    return server
