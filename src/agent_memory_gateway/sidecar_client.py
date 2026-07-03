"""Sidecar 到 Gateway 的 HTTP 客户端。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib import request

from .outbox import Outbox


class SidecarClient:
    """负责本机 agent 与 Memory Gateway 之间的转发。"""

    def __init__(self) -> None:
        self.gateway_url = os.environ.get("MEMORY_GATEWAY_URL", "http://127.0.0.1:8787").rstrip("/")
        home = Path(os.environ.get("MEMORY_HOME", Path.home() / ".shared-memory"))
        self.outbox = Outbox(home / "outbox.db")
        self.agent_id = os.environ.get("MEMORY_AGENT_ID", "unknown-agent")
        self.device_id = os.environ.get("MEMORY_DEVICE_ID", os.environ.get("COMPUTERNAME", "unknown-device"))
        self.profile = os.environ.get("MEMORY_PROFILE", "default")

    def remember(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload.setdefault("agent_id", self.agent_id)
        payload.setdefault("device_id", self.device_id)
        payload.setdefault("user_id", self.profile)
        try:
            return self._post("/v1/events", payload)
        except Exception as exc:  # noqa: BLE001
            event_id = self.outbox.enqueue(payload)
            return {"status": "queued", "event_id": event_id, "reason": str(exc)}

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload.setdefault("agent_id", self.agent_id)
        payload.setdefault("device_id", self.device_id)
        payload.setdefault("user_id", self.profile)
        return self._post("/v1/memories/search", payload)

    def context(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload.setdefault("agent_id", self.agent_id)
        payload.setdefault("device_id", self.device_id)
        payload.setdefault("user_id", self.profile)
        return self._post("/v1/context", payload)

    def feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/memories/feedback", payload)

    def forget(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/memories/forget", payload)

    def sync(self) -> dict[str, Any]:
        sent = 0
        errors: list[str] = []
        for event in self.outbox.list_events():
            try:
                self._post("/v1/events", event)
                self.outbox.remove(event["event_id"])
                sent += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                break
        return {"queued": self.outbox.count(), "sent": sent, "errors": errors}

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self.gateway_url + path,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with request.urlopen(req, timeout=8) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
