"""记忆结晶页的失效标记与显式重算。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable

from .auth import Principal
from .gbrain_backend import GBrainBackend
from .metadata_store import MetadataStoreError, PostgresEventLedger


VALID_SCOPES = frozenset({"user", "workspace", "device", "agent", "private"})


class CrystalError(ValueError):
    """可安全返回的结晶操作错误码。"""


def scope_binding_hash(
    tenant_id: str,
    user_id: str,
    workspace_id: str,
    scope: str,
    namespace_key: str,
) -> str:
    material = "\x1f".join(("crystal-v1", tenant_id, user_id, workspace_id, scope, namespace_key))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def mark_crystal_stale(connection: Any, binding_hash: str, server_revision: int) -> None:
    """来源变化只让已有结晶页失效；不删除页面和来源历史。"""

    if not binding_hash:
        return
    connection.execute(
        """
        UPDATE memory_crystals
        SET status = 'stale', stale_server_revision = %s, updated_at = now()
        WHERE scope_binding_hash = %s AND status = 'ready'
        """,
        (server_revision, binding_hash),
    )


class PostgresCrystalService:
    def __init__(
        self,
        metadata_dsn: str,
        gbrain: GBrainBackend,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not metadata_dsn:
            raise MetadataStoreError("缺少元数据库运行连接串")
        self._metadata_dsn = metadata_dsn
        self._gbrain = gbrain
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
        return self._psycopg().connect(self._metadata_dsn)

    def rebuild(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._text(payload.get("workspace_id"), "WORKSPACE_REQUIRED", 256)
        scope = self._text(payload.get("scope"), "CRYSTAL_SCOPE_REQUIRED", 32)
        if scope not in VALID_SCOPES:
            raise CrystalError("CRYSTAL_SCOPE_INVALID")
        namespace_key = self._text(payload.get("namespace_key"), "CRYSTAL_NAMESPACE_REQUIRED", 256)
        request_key = self._text(payload.get("idempotency_key"), "IDEMPOTENCY_KEY_REQUIRED", 256)
        binding_hash = scope_binding_hash(
            principal.tenant_id, principal.user_id, workspace_id, scope, namespace_key
        )
        with self._connect() as connection:
            with connection.transaction():
                PostgresEventLedger._require_binding(connection, principal, workspace_id, "memory.manage")
                rows = connection.execute(
                    """
                    SELECT backend_ref
                    FROM memory_lifecycle
                    WHERE tenant_id = %s AND user_id = %s AND workspace_id = %s
                      AND scope = %s AND namespace_key = %s
                      AND status = 'active' AND instruction_like = false
                    ORDER BY backend_ref ASC
                    FOR UPDATE
                    """,
                    (principal.tenant_id, principal.user_id, workspace_id, scope, namespace_key),
                ).fetchall()
                source_refs = [str(row[0]) for row in rows]
                if len(source_refs) < 2:
                    raise CrystalError("CRYSTAL_SOURCE_COUNT_INVALID")
                page_ref = self._gbrain.rebuild_crystal(
                    idempotency_key=self._gbrain_key(binding_hash, request_key),
                    tenant_id=principal.tenant_id,
                    source_refs=source_refs,
                    scope_binding_hash=binding_hash,
                )
                revision = self._next_revision(connection)
                connection.execute(
                    """
                    INSERT INTO memory_crystals (
                      scope_binding_hash, tenant_id, user_id, workspace_id, scope, namespace_key,
                      page_ref, source_refs, rule_version, status, generated_server_revision,
                      stale_server_revision, last_error_code
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'crystal-v1', 'ready', %s,
                              NULL, NULL)
                    ON CONFLICT (scope_binding_hash) DO UPDATE SET
                      page_ref = EXCLUDED.page_ref,
                      source_refs = EXCLUDED.source_refs,
                      rule_version = EXCLUDED.rule_version,
                      status = 'ready',
                      generated_server_revision = EXCLUDED.generated_server_revision,
                      stale_server_revision = NULL,
                      last_error_code = NULL,
                      updated_at = now()
                    """,
                    (
                        binding_hash, principal.tenant_id, principal.user_id, workspace_id,
                        scope, namespace_key, page_ref,
                        json.dumps(source_refs, separators=(",", ":")), revision,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO audit_log (
                      tenant_id, actor_type, actor_id, action, result_code, trace_id,
                      device_id, agent_installation_id, workspace_id, target_ref, details_json
                    ) VALUES (%s, 'device', %s, 'crystal.rebuilt', 'READY', %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        principal.tenant_id, principal.agent_installation_id, f"tr_{uuid.uuid4().hex}",
                        principal.device_id, principal.agent_installation_id, workspace_id, page_ref,
                        json.dumps({"scope_binding_hash": binding_hash, "source_count": len(source_refs)}, separators=(",", ":")),
                    ),
                )
        return {
            "status": "ready",
            "page_ref": page_ref,
            "scope_binding_hash": binding_hash,
            "source_count": len(source_refs),
            "server_revision": revision,
        }

    @staticmethod
    def _next_revision(connection: Any) -> int:
        connection.execute(
            """
            INSERT INTO gateway_state (state_key, state_value)
            VALUES ('server_revision', '0') ON CONFLICT (state_key) DO NOTHING
            """
        )
        row = connection.execute(
            "SELECT state_value FROM gateway_state WHERE state_key = 'server_revision' FOR UPDATE"
        ).fetchone()
        revision = int(row[0]) + 1
        connection.execute(
            "UPDATE gateway_state SET state_value = %s, updated_at = now() WHERE state_key = 'server_revision'",
            (str(revision),),
        )
        return revision

    @staticmethod
    def _gbrain_key(binding_hash: str, request_key: str) -> str:
        digest = hashlib.sha256(f"crystal:{binding_hash}:{request_key}".encode("utf-8")).hexdigest()
        return f"crystal-{digest}"

    @staticmethod
    def _text(value: Any, code: str, maximum: int) -> str:
        result = str(value or "").strip()
        if not result or len(result) > maximum:
            raise CrystalError(code)
        return result
