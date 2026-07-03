"""HTTP Memory Gateway。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .store import MemoryStore


class GatewayHandler(BaseHTTPRequestHandler):
    """最小 HTTP API 处理器。"""

    store: MemoryStore

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            self._json({"ok": True, "service": "agent-memory-gateway"})
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = self._read_json()
        try:
            if path == "/v1/events":
                self._json(self.store.record_event(payload))
            elif path == "/v1/memories/search":
                self._json({"memories": self.store.search(payload)})
            elif path == "/v1/context":
                self._json(self.store.context(payload))
            elif path == "/v1/memories/feedback":
                self._json(self.store.feedback(payload))
            elif path == "/v1/memories/forget":
                self._json(self.store.forget(payload))
            else:
                self._json({"error": "not found"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, status=400)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动多 Agent 共享记忆 Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default="memory.db")
    args = parser.parse_args()

    GatewayHandler.store = MemoryStore(Path(args.db))
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print(f"Memory Gateway listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
