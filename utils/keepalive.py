from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from typing import Any


class KeepAliveState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._status = "booting"
        self._details = "Starting Memact AutoMod."

    def set_status(self, status: str, details: str) -> None:
        with self._lock:
            self._status = status
            self._details = details

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "service": "Memact AutoMod",
                "status": self._status,
                "details": self._details,
                "uptime_seconds": int(time.time() - self._started_at),
                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }


def _resolve_keepalive_port() -> int | None:
    for key in ("PORT", "MEMACT_KEEPALIVE_PORT"):
        raw_value = os.getenv(key, "").strip()
        if not raw_value:
            continue
        try:
            return int(raw_value)
        except ValueError:
            print(f"Skipping keepalive server because {key} is invalid: {raw_value!r}")
            return None

    running_on_replit = any(os.getenv(key) for key in ("REPL_ID", "REPL_SLUG", "REPL_OWNER"))
    if running_on_replit:
        return 10000

    force_keepalive = os.getenv("MEMACT_ENABLE_KEEPALIVE", "").strip().lower()
    if force_keepalive in {"1", "true", "yes", "on"}:
        return 10000

    return None


def start_keepalive_server(state: KeepAliveState) -> ThreadingHTTPServer | None:
    port = _resolve_keepalive_port()
    if port is None:
        return None

    host = os.getenv("MEMACT_KEEPALIVE_HOST", "0.0.0.0").strip() or "0.0.0.0"

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
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="memact-keepalive")
    thread.start()
    print(f"Keepalive server listening on http://{host}:{port}")
    return server
