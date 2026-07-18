"""受现有管理能力保护的运行与授权概览。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .auth import Principal


class AdminServiceError(ValueError):
    """管理接口的稳定输入错误。"""


class PostgresAdminService:
    """只返回管理页面所需的元数据，不返回记忆正文或凭据。"""

    def __init__(self, dsn: str, *, connection_factory: Any | None = None) -> None:
        if not dsn:
            raise ValueError("缺少元数据库运行连接串")
        self._dsn = dsn
        self._connection_factory = connection_factory

    @staticmethod
    def _psycopg() -> Any:
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise RuntimeError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
        return psycopg

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        return self._psycopg().connect(self._dsn, autocommit=True)

    def overview(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._workspace_id(payload, principal)
        with self._connect() as connection:
            pending_reviews = self._count(
                connection,
                """
                SELECT COUNT(*)
                FROM review_candidates AS c
                JOIN gateway_events AS e ON e.device_id = c.device_id AND e.event_id = c.event_id
                WHERE e.tenant_id = %s AND e.user_id = %s AND e.workspace_id = %s
                  AND c.status = 'pending'
                """,
                (principal.tenant_id, principal.user_id, workspace_id),
            )
            retryable_events = self._count(
                connection,
                """
                SELECT COUNT(*) FROM gateway_events
                WHERE tenant_id = %s AND user_id = %s AND workspace_id = %s
                  AND status IN ('pending', 'retryable_failed')
                """,
                (principal.tenant_id, principal.user_id, workspace_id),
            )
            unresolved_dead_letters = self._count(
                connection,
                """
                SELECT COUNT(*)
                FROM dead_letters AS d
                JOIN gateway_events AS e ON e.device_id = d.device_id AND e.event_id = d.event_id
                WHERE e.tenant_id = %s AND e.user_id = %s AND e.workspace_id = %s
                  AND d.resolved_at IS NULL
                """,
                (principal.tenant_id, principal.user_id, workspace_id),
            )
            active_devices = self._count(
                connection,
                """
                SELECT COUNT(DISTINCT d.device_id)
                FROM devices AS d
                JOIN agent_installations AS a ON a.device_id = d.device_id
                JOIN workspace_bindings AS b ON b.agent_installation_id = a.agent_installation_id
                JOIN workspaces AS w ON w.workspace_id = b.workspace_id
                WHERE w.tenant_id = %s AND w.user_id = %s AND b.workspace_id = %s
                  AND d.status = 'active' AND a.status = 'active' AND b.status = 'active'
                """,
                (principal.tenant_id, principal.user_id, workspace_id),
            )
            heartbeat_row = connection.execute(
                "SELECT updated_at FROM gateway_state WHERE state_key = 'worker_heartbeat'"
            ).fetchone()
        return {
            "workspace_id": workspace_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "pending_reviews": pending_reviews,
                "retryable_events": retryable_events,
                "unresolved_dead_letters": unresolved_dead_letters,
                "active_devices": active_devices,
            },
            "worker_heartbeat_at": self._timestamp(self._value(heartbeat_row, 0, "updated_at")),
        }

    def list_devices(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._workspace_id(payload, principal)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.device_id, d.display_name AS device_name, d.device_type,
                       d.status AS device_status, d.auth_epoch AS device_auth_epoch,
                       d.last_seen_at AS device_last_seen_at,
                       a.agent_installation_id, a.display_name AS agent_name, a.agent_type,
                       a.status AS agent_status, a.auth_epoch AS agent_auth_epoch,
                       b.capabilities, b.status AS binding_status,
                       b.updated_at AS binding_updated_at
                FROM devices AS d
                JOIN agent_installations AS a ON a.device_id = d.device_id
                JOIN workspace_bindings AS b ON b.agent_installation_id = a.agent_installation_id
                JOIN workspaces AS w ON w.workspace_id = b.workspace_id
                WHERE w.tenant_id = %s AND w.user_id = %s AND b.workspace_id = %s
                ORDER BY d.display_name, a.display_name
                """,
                (principal.tenant_id, principal.user_id, workspace_id),
            ).fetchall()
        records = []
        for row in rows:
            records.append(
                {
                    "device_id": str(self._value(row, 0, "device_id")),
                    "device_name": str(self._value(row, 1, "device_name")),
                    "device_type": str(self._value(row, 2, "device_type")),
                    "device_status": str(self._value(row, 3, "device_status")),
                    "device_auth_epoch": int(self._value(row, 4, "device_auth_epoch")),
                    "device_last_seen_at": self._timestamp(self._value(row, 5, "device_last_seen_at")),
                    "agent_installation_id": str(self._value(row, 6, "agent_installation_id")),
                    "agent_name": str(self._value(row, 7, "agent_name")),
                    "agent_type": str(self._value(row, 8, "agent_type")),
                    "agent_status": str(self._value(row, 9, "agent_status")),
                    "agent_auth_epoch": int(self._value(row, 10, "agent_auth_epoch")),
                    "capabilities": sorted(str(value) for value in (self._value(row, 11, "capabilities") or [])),
                    "binding_status": str(self._value(row, 12, "binding_status")),
                    "binding_updated_at": self._timestamp(self._value(row, 13, "binding_updated_at")),
                }
            )
        return {"workspace_id": workspace_id, "devices": records}

    def list_audit(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._workspace_id(payload, principal)
        limit = self._limit(payload.get("limit"), default=50, maximum=100)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT audit_id, actor_type, actor_id, action, result_code, trace_id,
                       device_id, agent_installation_id, target_ref, audit.created_at
                FROM audit_log AS audit
                JOIN workspaces AS workspace ON workspace.workspace_id = audit.workspace_id
                WHERE audit.tenant_id = %s AND workspace.user_id = %s
                  AND audit.workspace_id = %s
                ORDER BY audit.created_at DESC, audit.audit_id DESC
                LIMIT %s
                """,
                (principal.tenant_id, principal.user_id, workspace_id, limit),
            ).fetchall()
        entries = []
        for row in rows:
            entries.append(
                {
                    "audit_id": int(self._value(row, 0, "audit_id")),
                    "actor_type": str(self._value(row, 1, "actor_type")),
                    "actor_id": str(self._value(row, 2, "actor_id")),
                    "action": str(self._value(row, 3, "action")),
                    "result_code": str(self._value(row, 4, "result_code")),
                    "trace_id": str(self._value(row, 5, "trace_id")),
                    "device_id": self._optional_text(self._value(row, 6, "device_id")),
                    "agent_installation_id": self._optional_text(
                        self._value(row, 7, "agent_installation_id")
                    ),
                    "target_ref": self._optional_text(self._value(row, 8, "target_ref")),
                    "created_at": self._timestamp(self._value(row, 9, "created_at")),
                }
            )
        return {"workspace_id": workspace_id, "entries": entries}

    def list_dead_letters(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """列出未处理死信的元数据，供运维排障使用。"""

        workspace_id = self._workspace_id(payload, principal)
        limit = self._limit(payload.get("limit"), default=50, maximum=100)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.dead_letter_id, d.event_id, d.error_code, d.last_error_class,
                       d.created_at, d.resolved_at, d.resolution_code
                FROM dead_letters AS d
                JOIN gateway_events AS e ON e.device_id = d.device_id AND e.event_id = d.event_id
                WHERE e.tenant_id = %s AND e.user_id = %s AND e.workspace_id = %s
                  AND d.resolved_at IS NULL
                ORDER BY d.created_at DESC, d.dead_letter_id DESC
                LIMIT %s
                """,
                (principal.tenant_id, principal.user_id, workspace_id, limit),
            ).fetchall()
        entries = []
        for row in rows:
            entries.append(
                {
                    "dead_letter_id": str(self._value(row, 0, "dead_letter_id")),
                    "event_id": str(self._value(row, 1, "event_id")),
                    "error_code": str(self._value(row, 2, "error_code")),
                    "error_class": str(self._value(row, 3, "error_class")),
                    "created_at": self._timestamp(self._value(row, 4, "created_at")),
                    "resolved_at": self._timestamp(self._value(row, 5, "resolved_at")),
                    "resolution_code": self._optional_text(self._value(row, 6, "resolution_code")),
                }
            )
        return {"workspace_id": workspace_id, "dead_letters": entries}

    @staticmethod
    def _workspace_id(payload: dict[str, Any], principal: Principal) -> str:
        workspace_id = str(payload.get("workspace_id") or "").strip()
        if not workspace_id:
            raise AdminServiceError("WORKSPACE_REQUIRED")
        principal.require_workspace(workspace_id)
        return workspace_id

    @staticmethod
    def _limit(value: Any, *, default: int, maximum: int) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise AdminServiceError("LIMIT_INVALID") from exc
        if not 1 <= parsed <= maximum:
            raise AdminServiceError("LIMIT_INVALID")
        return parsed

    @staticmethod
    def _count(connection: Any, sql: str, params: tuple[Any, ...]) -> int:
        row = connection.execute(sql, params).fetchone()
        return int(PostgresAdminService._value(row, 0, "count") or 0)

    @staticmethod
    def _value(row: Any, index: int, key: str) -> Any:
        if row is None:
            return None
        if isinstance(row, dict):
            return row.get(key)
        return row[index]

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return None if value is None else str(value)
