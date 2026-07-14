"""Gateway 事件账本：身份绑定、密文暂存、幂等与不含正文的审计。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from .auth import AuthError, Principal
from .crypto import EventCipher
from .event_contract import (
    EventValidationError,
    ProposedMemoryEvent,
    SensitiveContentError,
    parse_proposed_event,
)
from .security import SensitiveContentScanner


class MetadataStoreError(RuntimeError):
    """元数据库不可用或不满足 Gateway 契约。"""


class PostgresEventLedger:
    """每次调用独立连接，避免在 ThreadingHTTPServer 中共享裸连接。"""

    def __init__(
        self,
        dsn: str,
        cipher: EventCipher,
        security_scanner: SensitiveContentScanner | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise MetadataStoreError("缺少元数据库连接串")
        self._dsn = dsn
        self._cipher = cipher
        self._security_scanner = security_scanner or SensitiveContentScanner()
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
        return self._psycopg().connect(self._dsn)

    def record_proposed_event(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """写入 pending 事件；只有 GBrain 确认后才由 worker 改为 applied。"""

        trace_id = f"tr_{uuid.uuid4().hex}"
        try:
            event = parse_proposed_event(payload, principal, self._security_scanner)
        except SensitiveContentError as exc:
            self._audit_sensitive_rejection(payload, principal, exc, trace_id)
            raise
        with self._connect() as connection:
            with connection.transaction():
                self._require_binding(connection, principal, event.workspace_id, "memory.write_event")
                encrypted = self._cipher.encrypt_json(event.envelope(), aad=event.aad(principal))
                inserted = connection.execute(
                    """
                    INSERT INTO gateway_events (
                      device_id, event_id, tenant_id, user_id, agent_installation_id,
                      workspace_id, session_id, device_seq, event_type, schema_version,
                      causation_id, scope, payload_hash, payload_ciphertext, payload_nonce,
                      payload_key_version, instruction_like, status
                    ) VALUES (
                      %(device_id)s, %(event_id)s, %(tenant_id)s, %(user_id)s,
                      %(agent_installation_id)s, %(workspace_id)s, %(session_id)s,
                      %(device_seq)s, %(event_type)s, %(schema_version)s,
                      %(causation_id)s, %(scope)s, %(payload_hash)s, %(payload_ciphertext)s,
                      %(payload_nonce)s, %(payload_key_version)s, %(instruction_like)s, 'pending'
                    )
                    ON CONFLICT (device_id, event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    {
                        "device_id": principal.device_id,
                        "event_id": event.event_id,
                        "tenant_id": principal.tenant_id,
                        "user_id": principal.user_id,
                        "agent_installation_id": principal.agent_installation_id,
                        "workspace_id": event.workspace_id,
                        "session_id": event.session_id,
                        "device_seq": event.device_seq,
                        "event_type": event.event_type,
                        "schema_version": event.schema_version,
                        "causation_id": event.causation_id,
                        "scope": str(event.payload["requested_scope"]),
                        "payload_hash": event.payload_hash,
                        "payload_ciphertext": encrypted.ciphertext,
                        "payload_nonce": encrypted.nonce,
                        "payload_key_version": encrypted.key_version,
                        "instruction_like": bool(event.payload["instruction_like"]),
                    },
                ).fetchone()
                if inserted is not None:
                    self._audit(connection, principal, event.workspace_id, "event.accepted", "PENDING", trace_id, event.event_id)
                    return {
                        "event_id": event.event_id,
                        "status": "pending",
                        "retryable": True,
                        "trace_id": trace_id,
                    }

                existing = connection.execute(
                    """
                    SELECT e.payload_hash, e.status, e.result_code, e.error_code,
                           e.backend_ref, e.server_revision,
                           r.ack_id, r.status, r.result_code, r.error_code,
                           r.backend_ref, r.server_revision, r.trace_id, r.processed_at
                    FROM gateway_events AS e
                    LEFT JOIN event_receipts AS r
                      ON r.device_id = e.device_id AND r.event_id = e.event_id
                    WHERE e.device_id = %s AND e.event_id = %s
                    """,
                    (principal.device_id, event.event_id),
                ).fetchone()
                if existing is None:
                    raise MetadataStoreError("事件幂等读取失败")
                if existing[0] != event.payload_hash:
                    self._audit(connection, principal, event.workspace_id, "event.rejected", "EVENT_ID_REUSE", trace_id, event.event_id)
                    raise EventValidationError("EVENT_ID_REUSE")

                status = str(existing[1])
                if status in {"applied", "rejected"}:
                    if existing[6] is None:
                        raise MetadataStoreError("终态事件缺少固定回执")
                    result = {
                        "event_id": event.event_id,
                        "status": "duplicate",
                        "retryable": False,
                        "ack_id": str(existing[6]),
                        "trace_id": str(existing[12]),
                        "processed_at": existing[13].isoformat(),
                    }
                    if existing[8] is not None:
                        result["result"] = str(existing[8])
                    if existing[9] is not None:
                        result["error"] = str(existing[9])
                    if existing[10] is not None:
                        result["backend_ref"] = str(existing[10])
                    if existing[11] is not None:
                        result["server_revision"] = int(existing[11])
                    return result
                result = {
                    "event_id": event.event_id,
                    "status": status,
                    "retryable": status in {"pending", "retryable_failed"},
                    "trace_id": trace_id,
                }
                if existing[2] is not None:
                    result["result"] = existing[2]
                if existing[3] is not None:
                    result["error"] = existing[3]
                if existing[4] is not None:
                    result["backend_ref"] = existing[4]
                if existing[5] is not None:
                    result["server_revision"] = existing[5]
                return result

    def _audit_sensitive_rejection(
        self,
        payload: dict[str, Any],
        principal: Principal,
        error: SensitiveContentError,
        trace_id: str,
    ) -> None:
        workspace_id = str(payload.get("workspace_id") or "").strip()
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id or len(event_id) > 128:
            event_id = "evt_unidentified"
        with self._connect() as connection:
            with connection.transaction():
                self._require_binding(connection, principal, workspace_id, "memory.write_event")
                findings = error.assessment.sensitive_findings[:16]
                details = {
                    "categories": sorted({finding.category for finding in findings}),
                    "findings": [
                        {
                            "rule_id": finding.rule_id,
                            "fingerprint": finding.fingerprint,
                            "length_band": finding.length_band,
                        }
                        for finding in findings
                    ],
                    "rule_version": error.assessment.rule_version,
                }
                connection.execute(
                    """
                    INSERT INTO audit_log (
                      tenant_id, actor_type, actor_id, action, result_code, trace_id,
                      device_id, agent_installation_id, workspace_id, target_ref, details_json
                    ) VALUES (%s, 'device', %s, 'event.rejected_sensitive', 'SENSITIVE_CONTENT',
                              %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        principal.tenant_id,
                        principal.agent_installation_id,
                        trace_id,
                        principal.device_id,
                        principal.agent_installation_id,
                        workspace_id,
                        event_id,
                        json.dumps(details, sort_keys=True, separators=(",", ":")),
                    ),
                )

    @staticmethod
    def _require_binding(connection: Any, principal: Principal, workspace_id: str, capability: str) -> None:
        principal.require_workspace(workspace_id)
        principal.require_capability(capability)
        row = connection.execute(
            """
            SELECT 1
            FROM devices AS d
            JOIN agent_installations AS a ON a.device_id = d.device_id
            JOIN workspace_bindings AS wb ON wb.agent_installation_id = a.agent_installation_id
            JOIN workspaces AS w ON w.workspace_id = wb.workspace_id
            WHERE d.device_id = %s
              AND d.tenant_id = %s
              AND d.user_id = %s
              AND d.status = 'active'
              AND a.agent_installation_id = %s
              AND a.status = 'active'
              AND w.workspace_id = %s
              AND w.tenant_id = %s
              AND w.user_id = %s
              AND w.status = 'active'
              AND wb.status = 'active'
              AND wb.capabilities @> %s::text[]
            """,
            (
                principal.device_id,
                principal.tenant_id,
                principal.user_id,
                principal.agent_installation_id,
                workspace_id,
                principal.tenant_id,
                principal.user_id,
                [capability],
            ),
        ).fetchone()
        if row is None:
            raise AuthError("PRINCIPAL_BINDING_FORBIDDEN")

    @staticmethod
    def _audit(
        connection: Any,
        principal: Principal,
        workspace_id: str,
        action: str,
        result_code: str,
        trace_id: str,
        target_ref: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log (
              tenant_id, actor_type, actor_id, action, result_code, trace_id,
              device_id, agent_installation_id, workspace_id, target_ref
            ) VALUES (%s, 'device', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                principal.tenant_id,
                principal.agent_installation_id,
                action,
                result_code,
                trace_id,
                principal.device_id,
                principal.agent_installation_id,
                workspace_id,
                target_ref,
            ),
        )
