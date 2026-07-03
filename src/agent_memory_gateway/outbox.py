"""Sidecar 本地离线队列。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any


class Outbox:
    """离线时缓存 memory events。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox_events (
              id TEXT PRIMARY KEY,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.commit()

    def enqueue(self, payload: dict[str, Any]) -> str:
        event_id = str(payload.get("event_id") or f"evt_{uuid.uuid4().hex}")
        payload["event_id"] = event_id
        self.conn.execute(
            "INSERT OR IGNORE INTO outbox_events (id, payload_json) VALUES (?, ?)",
            (event_id, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()
        return event_id

    def list_events(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM outbox_events ORDER BY created_at ASC").fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def remove(self, event_id: str) -> None:
        self.conn.execute("DELETE FROM outbox_events WHERE id = ?", (event_id,))
        self.conn.commit()

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS count FROM outbox_events").fetchone()
        return int(row["count"])
