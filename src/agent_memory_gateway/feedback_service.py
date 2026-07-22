"""正式 PostgreSQL 模式下的记忆反馈持久化。"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from .auth import Principal
from .metadata_store import MetadataStoreError, PostgresEventLedger


ACTION_ALIASES = {
    "useful": "useful",
    "helpful": "useful",
    "pin": "pin",
    "stale": "outdated",
    "outdated": "outdated",
    "archive": "outdated",
    "wrong": "incorrect",
    "incorrect": "incorrect",
}


class PostgresFeedbackService:
    def __init__(
        self,
        metadata_dsn: str,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not metadata_dsn:
            raise MetadataStoreError("缺少元数据库运行连接串")
        self._metadata_dsn = metadata_dsn
        self._connection_factory = connection_factory

    @staticmethod
    def _psycopg() -> Any:
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise MetadataStoreError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
        return psycopg

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        return self._psycopg().connect(self._metadata_dsn, autocommit=True)

    def record(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = str(payload.get("workspace_id") or "").strip()
        if not workspace_id:
            if len(principal.workspace_ids) != 1:
                raise ValueError("WORKSPACE_ID_REQUIRED")
            workspace_id = next(iter(principal.workspace_ids))
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,256}", workspace_id):
            raise ValueError("WORKSPACE_ID_INVALID")
        principal.require_workspace_capability(workspace_id, "memory.feedback")
        memory_id = str(payload.get("memory_id") or "").strip()
        if not memory_id or len(memory_id) > 256:
            raise ValueError("MEMORY_ID_REQUIRED")
        action = ACTION_ALIASES.get(str(payload.get("action") or "").strip().lower())
        if action is None:
            raise ValueError("FEEDBACK_ACTION_UNSUPPORTED")
        recall_id = str(payload.get("recall_id") or "").strip() or None
        if recall_id is not None and len(recall_id) > 128:
            raise ValueError("RECALL_ID_INVALID")
        idempotency_key = str(payload.get("idempotency_key") or f"feedback_{uuid.uuid4().hex}").strip()
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("IDEMPOTENCY_KEY_INVALID")
        feedback_id = f"fb_{uuid.uuid4().hex}"

        with self._connect() as connection:
            PostgresEventLedger._require_binding(connection, principal, workspace_id, "memory.feedback")
            self._require_visible_memory(connection, principal, workspace_id, memory_id)
            if recall_id is not None:
                self._require_recall(connection, principal, workspace_id, memory_id, recall_id)
            inserted = connection.execute(
                """
                INSERT INTO memory_feedback_events (
                  feedback_id, tenant_id, user_id, workspace_id, device_id,
                  agent_installation_id, memory_id, recall_id, action, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, user_id, agent_installation_id, idempotency_key)
                DO NOTHING
                RETURNING feedback_id
                """,
                (
                    feedback_id,
                    principal.tenant_id,
                    principal.user_id,
                    workspace_id,
                    principal.device_id,
                    principal.agent_installation_id,
                    memory_id,
                    recall_id,
                    action,
                    idempotency_key,
                ),
            ).fetchone()
            if inserted is None:
                existing = connection.execute(
                    """
                    SELECT feedback_id, memory_id, recall_id, action
                    FROM memory_feedback_events
                    WHERE tenant_id = %s AND user_id = %s
                      AND agent_installation_id = %s AND idempotency_key = %s
                    """,
                    (
                        principal.tenant_id,
                        principal.user_id,
                        principal.agent_installation_id,
                        idempotency_key,
                    ),
                ).fetchone()
                if existing is None or (
                    str(existing[1]), str(existing[2] or "") or None, str(existing[3])
                ) != (memory_id, recall_id, action):
                    raise ValueError("IDEMPOTENCY_KEY_REUSED")
                feedback_id = str(existing[0])
                status = "duplicate"
            else:
                feedback_id = str(inserted[0])
                status = "recorded"
        return {
            "feedback_id": feedback_id,
            "memory_id": memory_id,
            "recall_id": recall_id,
            "action": action,
            "status": status,
        }

    @staticmethod
    def _require_visible_memory(
        connection: Any,
        principal: Principal,
        workspace_id: str,
        memory_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1
            FROM gateway_events AS event
            LEFT JOIN memory_lifecycle AS lifecycle ON lifecycle.backend_ref = event.backend_ref
            WHERE event.tenant_id = %s AND event.user_id = %s
              AND event.backend_ref = %s AND event.status = 'applied'
              AND COALESCE(lifecycle.status, 'active') = 'active'
              AND (
                event.scope = 'user'
                OR (event.scope = 'workspace' AND event.workspace_id = %s)
                OR (event.scope = 'device' AND event.device_id = %s)
                OR (event.scope = 'agent' AND event.agent_installation_id = %s)
                OR (event.scope = 'private' AND event.device_id = %s
                    AND event.agent_installation_id = %s)
              )
            LIMIT 1
            """,
            (
                principal.tenant_id,
                principal.user_id,
                memory_id,
                workspace_id,
                principal.device_id,
                principal.agent_installation_id,
                principal.device_id,
                principal.agent_installation_id,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("MEMORY_NOT_FOUND")

    @staticmethod
    def _require_recall(
        connection: Any,
        principal: Principal,
        workspace_id: str,
        memory_id: str,
        recall_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1
            FROM memory_recall_events
            WHERE recall_id = %s AND tenant_id = %s AND user_id = %s
              AND workspace_id = %s AND memory_refs @> %s::jsonb
            """,
            (
                recall_id,
                principal.tenant_id,
                principal.user_id,
                workspace_id,
                json.dumps([memory_id], separators=(",", ":")),
            ),
        ).fetchone()
        if row is None:
            raise ValueError("RECALL_REFERENCE_INVALID")
