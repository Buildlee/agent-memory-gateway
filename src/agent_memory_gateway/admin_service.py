"""受现有管理能力保护的运行与授权概览。"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from .auth import AuthError, Principal
from .identity_service import _audit


MANAGEABLE_CAPABILITIES = (
    "memory.feedback",
    "memory.forget",
    "memory.manage",
    "memory.read_context",
    "memory.search",
    "memory.sync",
    "memory.write_event",
)


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
                  AND d.last_seen_at IS NOT NULL
                  AND d.last_seen_at > now() - interval '15 minutes'
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
                    "is_current_device": str(self._value(row, 0, "device_id")) == principal.device_id,
                    "is_current_agent": str(self._value(row, 6, "agent_installation_id"))
                    == principal.agent_installation_id,
                }
            )
        return {
            "workspace_id": workspace_id,
            "capability_catalog": list(MANAGEABLE_CAPABILITIES),
            "devices": records,
        }

    def list_audit(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._workspace_id(payload, principal)
        limit = self._limit(payload.get("limit"), default=50, maximum=100)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT audit.audit_id, audit.actor_type, audit.actor_id, audit.action,
                       audit.result_code, audit.trace_id, audit.device_id,
                       audit.agent_installation_id, audit.target_ref, audit.created_at,
                       source_device.display_name AS source_device_name,
                       source_device.device_type AS source_device_type,
                       source_device.status AS source_device_status,
                       source_agent.display_name AS source_agent_name,
                       source_agent.agent_type AS source_agent_type,
                       source_agent.status AS source_agent_status
                FROM audit_log AS audit
                JOIN workspaces AS workspace ON workspace.workspace_id = audit.workspace_id
                LEFT JOIN agent_installations AS source_agent
                  ON source_agent.agent_installation_id = audit.agent_installation_id
                LEFT JOIN devices AS source_device
                  ON source_device.device_id = COALESCE(audit.device_id, source_agent.device_id)
                 AND source_device.tenant_id = audit.tenant_id
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
                    "source_device_name": self._optional_text(
                        self._value(row, 10, "source_device_name")
                    ),
                    "source_device_type": self._optional_text(
                        self._value(row, 11, "source_device_type")
                    ),
                    "source_device_status": self._optional_text(
                        self._value(row, 12, "source_device_status")
                    ),
                    "source_agent_name": self._optional_text(
                        self._value(row, 13, "source_agent_name")
                    ),
                    "source_agent_type": self._optional_text(
                        self._value(row, 14, "source_agent_type")
                    ),
                    "source_agent_status": self._optional_text(
                        self._value(row, 15, "source_agent_status")
                    ),
                }
            )
        return {"workspace_id": workspace_id, "entries": entries}

    def update_binding(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """更新当前工作区内某个 Agent 的能力，并防止管理端锁死自身。"""

        workspace_id = self._workspace_id(payload, principal)
        self._require_confirmation(payload)
        agent_id = self._required_text(
            payload,
            "target_agent_installation_id",
            "TARGET_AGENT_INSTALLATION_ID_REQUIRED",
        )
        idempotency_key = self._required_text(payload, "idempotency_key", "IDEMPOTENCY_KEY_REQUIRED")
        expected = self._existing_capabilities(
            payload.get("expected_capabilities"),
            "EXPECTED_CAPABILITIES_INVALID",
        )
        capabilities = self._existing_capabilities(
            payload.get("capabilities"),
            "WORKSPACE_CAPABILITIES_INVALID",
        )
        allowed = set(MANAGEABLE_CAPABILITIES)
        if set(expected).difference(allowed) != set(capabilities).difference(allowed):
            raise AdminServiceError("WORKSPACE_CAPABILITIES_INVALID")
        if agent_id == principal.agent_installation_id and "memory.manage" not in capabilities:
            raise AuthError("ADMIN_SELF_LOCKOUT_FORBIDDEN", status=409)

        with self._connect() as connection:
            with self._transaction(connection):
                row = connection.execute(
                    """
                    SELECT d.device_id, d.display_name AS device_name, d.status AS device_status,
                           a.display_name AS agent_name, a.status AS agent_status,
                           b.capabilities, b.status AS binding_status
                    FROM workspace_bindings AS b
                    JOIN agent_installations AS a
                      ON a.agent_installation_id = b.agent_installation_id
                    JOIN devices AS d ON d.device_id = a.device_id
                    JOIN workspaces AS w ON w.workspace_id = b.workspace_id
                    WHERE b.agent_installation_id = %s AND b.workspace_id = %s
                      AND w.tenant_id = %s AND w.user_id = %s
                      AND d.tenant_id = %s AND d.user_id = %s
                    FOR UPDATE OF b, a, d
                    """,
                    (
                        agent_id,
                        workspace_id,
                        principal.tenant_id,
                        principal.user_id,
                        principal.tenant_id,
                        principal.user_id,
                    ),
                ).fetchone()
                if row is None:
                    raise AuthError("ADMIN_TARGET_NOT_FOUND", status=404)
                current = tuple(sorted(str(value) for value in (self._value(row, 5, "capabilities") or [])))
                if tuple(expected) != current:
                    raise AuthError("ADMIN_STATE_CHANGED", status=409)
                if any(
                    self._value(row, index, key) != "active"
                    for index, key in ((2, "device_status"), (4, "agent_status"), (6, "binding_status"))
                ):
                    raise AuthError("ADMIN_TARGET_INACTIVE", status=409)
                if tuple(capabilities) != current:
                    connection.execute(
                        """
                        UPDATE workspace_bindings
                        SET capabilities = %s, updated_at = now()
                        WHERE agent_installation_id = %s AND workspace_id = %s
                        """,
                        (list(capabilities), agent_id, workspace_id),
                    )
                    _audit(
                        connection,
                        tenant_id=principal.tenant_id,
                        actor_type="agent",
                        actor_id=principal.agent_installation_id,
                        action="auth.workspace.capabilities.update",
                        result_code="updated",
                        device_id=str(self._value(row, 0, "device_id")),
                        agent_installation_id=agent_id,
                        workspace_id=workspace_id,
                        target_ref=workspace_id,
                        details={
                            "before": list(current),
                            "after": list(capabilities),
                            "idempotency_key": idempotency_key,
                        },
                    )
                    status = "updated"
                else:
                    status = "unchanged"
        return {
            "workspace_id": workspace_id,
            "agent_installation_id": agent_id,
            "capabilities": list(capabilities),
            "status": status,
        }

    def revoke_agent(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """撤销一个 Agent；该动作会影响它在所有工作区的访问。"""

        workspace_id = self._workspace_id(payload, principal)
        self._require_confirmation(payload)
        agent_id = self._required_text(
            payload,
            "target_agent_installation_id",
            "TARGET_AGENT_INSTALLATION_ID_REQUIRED",
        )
        expected_epoch = self._positive_int(payload.get("expected_auth_epoch"), "EXPECTED_AUTH_EPOCH_REQUIRED")
        idempotency_key = self._required_text(payload, "idempotency_key", "IDEMPOTENCY_KEY_REQUIRED")
        if agent_id == principal.agent_installation_id:
            raise AuthError("ADMIN_SELF_REVOKE_FORBIDDEN", status=409)

        with self._connect() as connection:
            with self._transaction(connection):
                row = connection.execute(
                    """
                    SELECT d.device_id, d.status AS device_status,
                           a.status AS agent_status, a.auth_epoch AS agent_auth_epoch
                    FROM agent_installations AS a
                    JOIN devices AS d ON d.device_id = a.device_id
                    JOIN workspace_bindings AS b ON b.agent_installation_id = a.agent_installation_id
                    JOIN workspaces AS w ON w.workspace_id = b.workspace_id
                    WHERE a.agent_installation_id = %s AND b.workspace_id = %s
                      AND w.tenant_id = %s AND w.user_id = %s
                      AND d.tenant_id = %s AND d.user_id = %s
                    FOR UPDATE OF a, d
                    """,
                    (
                        agent_id,
                        workspace_id,
                        principal.tenant_id,
                        principal.user_id,
                        principal.tenant_id,
                        principal.user_id,
                    ),
                ).fetchone()
                if row is None:
                    raise AuthError("ADMIN_TARGET_NOT_FOUND", status=404)
                if int(self._value(row, 3, "agent_auth_epoch")) != expected_epoch:
                    raise AuthError("ADMIN_STATE_CHANGED", status=409)
                if self._value(row, 1, "device_status") != "active" or self._value(row, 2, "agent_status") != "active":
                    raise AuthError("ADMIN_TARGET_INACTIVE", status=409)
                changed = connection.execute(
                    """
                    UPDATE agent_installations
                    SET status = 'revoked', auth_epoch = auth_epoch + 1, updated_at = now()
                    WHERE agent_installation_id = %s AND auth_epoch = %s AND status = 'active'
                    RETURNING auth_epoch
                    """,
                    (agent_id, expected_epoch),
                ).fetchone()
                if changed is None:
                    raise AuthError("ADMIN_STATE_CHANGED", status=409)
                device_id = str(self._value(row, 0, "device_id"))
                _audit(
                    connection,
                    tenant_id=principal.tenant_id,
                    actor_type="agent",
                    actor_id=principal.agent_installation_id,
                    action="auth.agent.revoke",
                    result_code="revoked",
                    device_id=device_id,
                    agent_installation_id=agent_id,
                    workspace_id=workspace_id,
                    target_ref=agent_id,
                    details={"idempotency_key": idempotency_key},
                )
        return {
            "workspace_id": workspace_id,
            "device_id": device_id,
            "agent_installation_id": agent_id,
            "auth_epoch": int(self._value(changed, 0, "auth_epoch")),
            "status": "revoked",
        }

    def revoke_device(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """撤销设备、设备上的 Agent 和刷新凭据，不删除任何记录。"""

        workspace_id = self._workspace_id(payload, principal)
        self._require_confirmation(payload)
        device_id = self._required_text(payload, "target_device_id", "TARGET_DEVICE_ID_REQUIRED")
        expected_epoch = self._positive_int(payload.get("expected_auth_epoch"), "EXPECTED_AUTH_EPOCH_REQUIRED")
        idempotency_key = self._required_text(payload, "idempotency_key", "IDEMPOTENCY_KEY_REQUIRED")
        if device_id == principal.device_id:
            raise AuthError("ADMIN_SELF_REVOKE_FORBIDDEN", status=409)

        with self._connect() as connection:
            with self._transaction(connection):
                row = connection.execute(
                    """
                    SELECT d.status AS device_status, d.auth_epoch AS device_auth_epoch
                    FROM devices AS d
                    JOIN agent_installations AS a ON a.device_id = d.device_id
                    JOIN workspace_bindings AS b ON b.agent_installation_id = a.agent_installation_id
                    JOIN workspaces AS w ON w.workspace_id = b.workspace_id
                    WHERE d.device_id = %s AND b.workspace_id = %s
                      AND w.tenant_id = %s AND w.user_id = %s
                      AND d.tenant_id = %s AND d.user_id = %s
                    LIMIT 1
                    FOR UPDATE OF d
                    """,
                    (
                        device_id,
                        workspace_id,
                        principal.tenant_id,
                        principal.user_id,
                        principal.tenant_id,
                        principal.user_id,
                    ),
                ).fetchone()
                if row is None:
                    raise AuthError("ADMIN_TARGET_NOT_FOUND", status=404)
                if int(self._value(row, 1, "device_auth_epoch")) != expected_epoch:
                    raise AuthError("ADMIN_STATE_CHANGED", status=409)
                if self._value(row, 0, "device_status") != "active":
                    raise AuthError("ADMIN_TARGET_INACTIVE", status=409)
                changed = connection.execute(
                    """
                    UPDATE devices
                    SET status = 'revoked', revoked_at = now(),
                        auth_epoch = auth_epoch + 1, updated_at = now()
                    WHERE device_id = %s AND auth_epoch = %s AND status = 'active'
                    RETURNING auth_epoch
                    """,
                    (device_id, expected_epoch),
                ).fetchone()
                if changed is None:
                    raise AuthError("ADMIN_STATE_CHANGED", status=409)
                connection.execute(
                    """
                    UPDATE agent_installations
                    SET status = 'revoked', auth_epoch = auth_epoch + 1, updated_at = now()
                    WHERE device_id = %s AND status <> 'revoked'
                    """,
                    (device_id,),
                )
                connection.execute(
                    """
                    UPDATE refresh_credentials
                    SET revoked_at = now()
                    WHERE device_id = %s AND revoked_at IS NULL
                    """,
                    (device_id,),
                )
                _audit(
                    connection,
                    tenant_id=principal.tenant_id,
                    actor_type="agent",
                    actor_id=principal.agent_installation_id,
                    action="auth.device.revoke",
                    result_code="revoked",
                    device_id=device_id,
                    agent_installation_id=principal.agent_installation_id,
                    workspace_id=workspace_id,
                    target_ref=device_id,
                    details={"idempotency_key": idempotency_key},
                )
        return {
            "workspace_id": workspace_id,
            "device_id": device_id,
            "auth_epoch": int(self._value(changed, 0, "auth_epoch")),
            "status": "revoked",
        }

    def list_memories(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """返回工作区内所有可见记忆，含来源设备和生命周期状态。"""

        workspace_id = self._workspace_id(payload, principal)
        limit = self._limit(payload.get("limit"), default=200, maximum=500)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event.backend_ref, event.event_id, event.scope,
                       event.device_id, event.agent_installation_id,
                       event.received_at, event.server_revision,
                       lifecycle.status AS lifecycle_status,
                       lifecycle.superseded_by, lifecycle.confidence,
                       lifecycle.instruction_like
                FROM gateway_events AS event
                LEFT JOIN memory_lifecycle AS lifecycle
                  ON lifecycle.backend_ref = event.backend_ref
                WHERE event.tenant_id = %s
                  AND event.user_id = %s
                  AND event.status = 'applied'
                  AND event.backend_ref IS NOT NULL
                  AND (
                    event.scope = 'user'
                    OR (event.scope = 'workspace' AND event.workspace_id = %s)
                  )
                ORDER BY event.server_revision DESC
                LIMIT %s
                """,
                (principal.tenant_id, principal.user_id, workspace_id, limit),
            ).fetchall()
        memories = []
        for row in rows:
            memories.append(
                {
                    "backend_ref": str(self._value(row, 0, "backend_ref")),
                    "event_id": str(self._value(row, 1, "event_id")),
                    "scope": str(self._value(row, 2, "scope")),
                    "source_device_id": str(self._value(row, 3, "device_id")),
                    "source_agent_id": str(self._value(row, 4, "agent_installation_id")),
                    "received_at": self._timestamp(self._value(row, 5, "received_at")),
                    "server_revision": int(self._value(row, 6, "server_revision") or 0),
                    "lifecycle_status": str(self._value(row, 7, "lifecycle_status") or "active"),
                    "superseded_by": self._optional_text(self._value(row, 8, "superseded_by")),
                    "confidence": float(self._value(row, 9, "confidence") or 0),
                    "instruction_like": bool(self._value(row, 10, "instruction_like")),
                }
            )
        return {"workspace_id": workspace_id, "memories": memories}

    def memory_graph(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """返回工作区记忆实体关系图谱数据，用于可视化。"""

        workspace_id = self._workspace_id(payload, principal)
        with self._connect() as connection:
            event_rows = connection.execute(
                """
                SELECT backend_ref, device_id, agent_installation_id,
                       scope FROM gateway_events
                WHERE tenant_id = %s AND user_id = %s
                  AND backend_ref IS NOT NULL AND status = 'applied'
                  AND (scope = 'workspace' AND workspace_id = %s OR scope = 'user')
                ORDER BY server_revision DESC LIMIT 300
                """,
                (principal.tenant_id, principal.user_id, workspace_id),
            ).fetchall()
            lifecycle_rows = connection.execute(
                """
                SELECT backend_ref, lifecycle.status, superseded_by
                FROM memory_lifecycle lifecycle
                JOIN gateway_events event
                  ON event.backend_ref = lifecycle.backend_ref
                WHERE event.tenant_id = %s AND event.user_id = %s
                  AND (event.scope = 'workspace' AND event.workspace_id = %s OR event.scope = 'user')
                """,
                (principal.tenant_id, principal.user_id, workspace_id),
            ).fetchall()
        nodes = []
        edges = []
        seen = set()
        agent_ids = set()
        device_ids = set()
        for row in event_rows:
            ref = str(self._value(row, 0, "backend_ref"))
            if ref in seen:
                continue
            seen.add(ref)
            device = str(self._value(row, 1, "device_id") or "")
            agent = str(self._value(row, 2, "agent_installation_id") or "")
            scope = str(self._value(row, 3, "scope") or "")
            nodes.append({"id": ref, "label": ref.split(":")[-1] if ":" in ref else ref, "group": "memory", "scope": scope})
            if device:
                device_ids.add(device)
                edges.append({"from": ref, "to": f"device:{device}", "label": "from"})
            if agent:
                agent_ids.add(agent)
        for device in device_ids:
            nodes.append({"id": f"device:{device}", "label": device, "group": "device"})
        for agent in agent_ids:
            nodes.append({"id": f"agent:{agent}", "label": agent, "group": "agent"})
        life_edges = 0
        for row in lifecycle_rows:
            ref = str(self._value(row, 0, "backend_ref"))
            status = str(self._value(row, 1, "status") or "")
            sup = self._optional_text(self._value(row, 2, "superseded_by"))
            if sup:
                edges.append({"from": sup, "to": ref, "label": "supersedes", "dashes": True})
                life_edges += 1
            if status == "archived":
                node = next((n for n in nodes if n["id"] == ref), None)
                if node:
                    node["status"] = "archived"
        return {"workspace_id": workspace_id, "nodes": nodes, "edges": edges}

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

    @staticmethod
    def _transaction(connection: Any) -> Any:
        transaction = getattr(connection, "transaction", None)
        return transaction() if callable(transaction) else nullcontext()

    @staticmethod
    def _required_text(payload: dict[str, Any], key: str, code: str, maximum: int = 256) -> str:
        value = str(payload.get(key) or "").strip()
        if not value or len(value) > maximum:
            raise AdminServiceError(code)
        return value

    @staticmethod
    def _positive_int(value: Any, code: str) -> int:
        if isinstance(value, bool):
            raise AdminServiceError(code)
        try:
            converted = int(value)
        except (TypeError, ValueError) as exc:
            raise AdminServiceError(code) from exc
        if converted <= 0:
            raise AdminServiceError(code)
        return converted

    @staticmethod
    def _capabilities(value: Any, code: str) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise AdminServiceError(code)
        capabilities = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        if not capabilities or any(item not in MANAGEABLE_CAPABILITIES for item in capabilities):
            raise AdminServiceError(code)
        return capabilities

    @staticmethod
    def _existing_capabilities(value: Any, code: str) -> tuple[str, ...]:
        """允许原样携带既有扩展能力，但不允许页面增删它们。"""

        if not isinstance(value, list):
            raise AdminServiceError(code)
        capabilities = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
        if (
            not capabilities
            or len(capabilities) > 32
            or any(
                len(item) > 128
                or not item.replace(".", "").replace("_", "").isalnum()
                for item in capabilities
            )
        ):
            raise AdminServiceError(code)
        return capabilities

    @staticmethod
    def _require_confirmation(payload: dict[str, Any]) -> None:
        if not bool(payload.get("confirmed_by_user")):
            raise AdminServiceError("USER_CONFIRMATION_REQUIRED")
