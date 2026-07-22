"""审核、冲突和补偿服务。

正文只保留在加密候选和 GBrain；本模块在完成所有者与工作区校验后才解密候选，
元数据账本仅记录引用、状态、哈希和不可变操作历史。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .auth import Principal
from .crypto import EncryptedPayload, EncryptionError, EventCipher
from .crystal_service import mark_crystal_stale, scope_binding_hash
from .gbrain_backend import GBrainBackend
from .metadata_store import MetadataStoreError, PostgresEventLedger
from .security import SensitiveContentScanner


REVIEW_ACTIONS = frozenset(
    {"confirm", "confirm_edit", "retain_both", "supersede", "archive", "reject"}
)
EVIDENCE_RANK = {
    "user_confirmed": 4,
    "user_explicit": 4,
    "tool_verified": 3,
    "agent_inferred": 2,
    "agent_observed": 2,
    "imported": 1,
}


class ReviewError(ValueError):
    """可安全返回给管理端的稳定审核错误码。"""


@dataclass(frozen=True)
class ReviewCandidate:
    review_id: str
    revision: int
    status: str
    expires_at: Any
    created_at: Any
    origin: Principal
    origin_event_id: str
    workspace_id: str
    scope: str
    last_operation_id: str | None
    payload: dict[str, Any]


class PostgresReviewService:
    """用 Gateway 元数据账本协调人工审核与 GBrain 生命周期操作。"""

    def __init__(
        self,
        metadata_dsn: str,
        cipher: EventCipher,
        gbrain: GBrainBackend,
        *,
        security_scanner: SensitiveContentScanner | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> str:
        if not metadata_dsn:
            raise MetadataStoreError("缺少元数据库运行连接串")
        self._metadata_dsn = metadata_dsn
        self._cipher = cipher
        self._gbrain = gbrain
        self._scanner = security_scanner or SensitiveContentScanner()
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

    def list_pending(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._required_text(payload.get("workspace_id"), "WORKSPACE_REQUIRED", 256)
        limit = self._bounded_int(payload.get("limit", 30), "LIMIT_INVALID", 1, 100)
        with self._connect() as connection:
            with connection.transaction():
                self._require_review_binding(connection, principal, workspace_id)
                rows = connection.execute(
                    """
                    SELECT c.review_id, c.revision, c.status, c.expires_at, c.created_at,
                           e.device_id, e.event_id, e.tenant_id, e.user_id,
                           e.agent_installation_id, e.workspace_id, e.scope,
                           c.candidate_ciphertext, c.candidate_nonce, c.candidate_key_version,
                           c.last_operation_id
                    FROM review_candidates AS c
                    JOIN gateway_events AS e
                      ON e.device_id = c.device_id AND e.event_id = c.event_id
                    WHERE c.status = 'pending'
                      AND e.tenant_id = %s
                      AND e.user_id = %s
                      AND e.workspace_id = %s
                    ORDER BY c.created_at ASC
                    LIMIT %s
                    """,
                    (principal.tenant_id, principal.user_id, workspace_id, limit),
                ).fetchall()
                items = []
                for row in rows:
                    candidate = self._candidate_from_row(row)
                    conflicts = self._find_conflicts(connection, candidate, candidate.payload)
                    items.append(self._present_candidate(candidate, conflicts))
        return {"reviews": items, "count": len(items)}

    def resolve(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._required_text(payload.get("workspace_id"), "WORKSPACE_REQUIRED", 256)
        review_id = self._required_text(payload.get("review_id"), "REVIEW_ID_REQUIRED", 128)
        action = str(payload.get("action") or "").strip()
        if action not in REVIEW_ACTIONS:
            raise ReviewError("REVIEW_ACTION_INVALID")
        expected_revision = self._positive_int(payload.get("expected_revision"), "EXPECTED_REVISION_REQUIRED")
        idempotency_key = self._required_text(
            payload.get("idempotency_key"), "IDEMPOTENCY_KEY_REQUIRED", 256
        )
        with self._connect() as connection:
            with connection.transaction():
                self._require_review_binding(connection, principal, workspace_id)
                duplicate = self._existing_operation(
                    connection, idempotency_key, principal, workspace_id
                )
                if duplicate is not None:
                    return duplicate | {"idempotent": True}
                candidate = self._load_for_update(connection, review_id)
                self._require_candidate_owner(candidate, principal, workspace_id)
                self._require_expected_revision(candidate, expected_revision)
                if candidate.status != "pending":
                    raise ReviewError("REVIEW_NOT_PENDING")

                resolved_payload = self._resolved_payload(candidate, payload, action)
                conflicts = self._find_conflicts(connection, candidate, resolved_payload, for_update=True)
                if action in {"confirm", "confirm_edit"} and conflicts:
                    return {
                        "status": "conflict",
                        "review_id": candidate.review_id,
                        "review_revision": candidate.revision,
                        "suggested_action": self._suggest_action(resolved_payload, conflicts),
                        "conflicts": conflicts,
                        "message": "存在同一命名空间的活动记忆，请选择保留双方或明确取代。",
                    }
                if action == "supersede":
                    self._validate_supersede_target(payload, conflicts)
                    return self._confirm(
                        connection,
                        candidate,
                        principal,
                        action,
                        idempotency_key,
                        resolved_payload,
                        target_ref=str(payload["target_ref"]),
                    )
                if action in {"confirm", "confirm_edit", "retain_both"}:
                    return self._confirm(
                        connection,
                        candidate,
                        principal,
                        action,
                        idempotency_key,
                        resolved_payload,
                    )
                return self._close_without_fact(
                    connection, candidate, principal, action, idempotency_key
                )

    def forget(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._required_text(payload.get("workspace_id"), "WORKSPACE_REQUIRED", 256)
        backend_ref = self._required_text(payload.get("memory_id"), "MEMORY_ID_REQUIRED", 128)
        if bool(payload.get("hard_delete")):
            raise ReviewError("HARD_DELETE_NOT_SUPPORTED")
        with self._connect() as connection:
            with connection.transaction():
                PostgresEventLedger._require_binding(connection, principal, workspace_id, "memory.forget")
                row = connection.execute(
                    """
                    SELECT status, updated_server_revision
                    FROM memory_lifecycle
                    WHERE backend_ref = %s AND tenant_id = %s AND user_id = %s AND workspace_id = %s
                    FOR UPDATE
                    """,
                    (backend_ref, principal.tenant_id, principal.user_id, workspace_id),
                ).fetchone()
                if row is None:
                    raise ReviewError("MEMORY_NOT_FOUND")
                if str(row[0]) != "active":
                    return {"memory_id": backend_ref, "status": str(row[0]), "server_revision": int(row[1])}
                self._gbrain.archive(
                    idempotency_key=self._gbrain_key("forget", backend_ref, workspace_id), reference=backend_ref
                )
                server_revision = self._next_revision(connection)
                connection.execute(
                    """
                    UPDATE memory_lifecycle
                    SET status = 'archived', updated_server_revision = %s, updated_at = now()
                    WHERE backend_ref = %s
                    """,
                    (server_revision, backend_ref),
                )
                self._insert_tombstone(connection, backend_ref, principal, server_revision, "forgotten")
                self._mark_backend_crystal_stale(connection, backend_ref, server_revision)
        return {"memory_id": backend_ref, "status": "archived", "server_revision": server_revision}

    def revert(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = self._required_text(payload.get("workspace_id"), "WORKSPACE_REQUIRED", 256)
        review_id = self._required_text(payload.get("review_id"), "REVIEW_ID_REQUIRED", 128)
        operation_id = self._required_text(payload.get("operation_id"), "OPERATION_ID_REQUIRED", 128)
        expected_revision = self._positive_int(payload.get("expected_revision"), "EXPECTED_REVISION_REQUIRED")
        idempotency_key = self._required_text(
            payload.get("idempotency_key"), "IDEMPOTENCY_KEY_REQUIRED", 256
        )
        with self._connect() as connection:
            with connection.transaction():
                self._require_review_binding(connection, principal, workspace_id)
                duplicate = self._existing_operation(
                    connection, idempotency_key, principal, workspace_id
                )
                if duplicate is not None:
                    return duplicate | {"idempotent": True}
                candidate = self._load_for_update(connection, review_id)
                self._require_candidate_owner(candidate, principal, workspace_id)
                self._require_expected_revision(candidate, expected_revision)
                if candidate.last_operation_id != operation_id:
                    raise ReviewError("REVIEW_REVERT_NOT_LATEST")
                original = connection.execute(
                    """
                    SELECT operation_id, action, backend_ref, target_ref
                    FROM review_operations
                    WHERE operation_id = %s AND review_id = %s
                    FOR UPDATE
                    """,
                    (operation_id, review_id),
                ).fetchone()
                if original is None or str(original[1]) == "revert":
                    raise ReviewError("REVIEW_OPERATION_NOT_REVERSIBLE")
                already = connection.execute(
                    """
                    SELECT 1 FROM review_operations
                    WHERE compensates_operation_id = %s AND action = 'revert'
                    """,
                    (operation_id,),
                ).fetchone()
                if already is not None:
                    raise ReviewError("REVIEW_ALREADY_REVERTED")
                return self._revert_operation(
                    connection,
                    candidate,
                    principal,
                    tuple(original),
                    idempotency_key,
                )

    def _confirm(
        self,
        connection: Any,
        candidate: ReviewCandidate,
        principal: Principal,
        action: str,
        idempotency_key: str,
        resolved_payload: dict[str, Any],
        *,
        target_ref: str | None = None,
    ) -> dict[str, Any]:
        operation_id = f"rvop_{uuid.uuid4().hex}"
        create_key = self._gbrain_key("create", candidate.review_id, idempotency_key)
        backend_ref = self._gbrain.upsert_confirmed(
            idempotency_key=create_key,
            tenant_id=principal.tenant_id,
            content=str(resolved_payload["content"]),
            kind=str(resolved_payload["kind"]),
            confidence=float(resolved_payload["confidence"]),
            allow_instruction_like=bool(resolved_payload["approve_instruction_like"]),
        )
        if target_ref is not None:
            self._gbrain.supersede(
                idempotency_key=self._gbrain_key("supersede", candidate.review_id, idempotency_key),
                old_ref=target_ref,
                new_ref=backend_ref,
            )
        server_revision = self._next_revision(connection)
        result_code = "confirmed_superseding" if target_ref else "confirmed"
        result = {
            "status": "confirmed",
            "review_id": candidate.review_id,
            "review_revision": candidate.revision + 1,
            "server_revision": server_revision,
            "operation_id": operation_id,
            "backend_ref": backend_ref,
            "action": action,
        }
        if target_ref is not None:
            result["superseded_ref"] = target_ref
        self._record_operation(
            connection,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            candidate=candidate,
            action=action,
            result_code=result_code,
            result=result,
            backend_ref=backend_ref,
            target_ref=target_ref,
            content_hash=self._content_hash(resolved_payload),
        )
        binding_hash = self._insert_confirmed_lifecycle(
            connection,
            candidate,
            backend_ref,
            resolved_payload,
            server_revision,
            operation_id,
        )
        if target_ref is not None:
            self._mark_superseded(
                connection, candidate, target_ref, backend_ref, server_revision, operation_id, binding_hash
            )
        connection.execute(
            """
            UPDATE review_candidates
            SET status = 'confirmed', revision = revision + 1, resolved_at = now(),
                resolved_by = %s, last_operation_id = %s, updated_at = now()
            WHERE review_id = %s AND revision = %s
            """,
            (principal.user_id, operation_id, candidate.review_id, candidate.revision),
        )
        connection.execute(
            """
            UPDATE gateway_events
            SET backend_ref = %s, result_code = 'candidate_confirmed', server_revision = %s
            WHERE device_id = %s AND event_id = %s
            """,
            (backend_ref, server_revision, candidate.origin.device_id, candidate.origin_event_id),
        )
        connection.execute(
            """
            INSERT INTO backend_bindings (
              idempotency_key, device_id, event_id, backend_name, backend_ref, payload_hash
            )
            SELECT %s, device_id, event_id, 'gbrain', %s, payload_hash
            FROM gateway_events WHERE device_id = %s AND event_id = %s
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (self._metadata_binding_key(candidate, operation_id), backend_ref,
             candidate.origin.device_id, candidate.origin_event_id),
        )
        self._audit(connection, principal, candidate, f"review.{action}", result_code, result)
        return result

    def _close_without_fact(
        self,
        connection: Any,
        candidate: ReviewCandidate,
        principal: Principal,
        action: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        status = "archived" if action == "archive" else "rejected"
        operation_id = f"rvop_{uuid.uuid4().hex}"
        server_revision = self._next_revision(connection)
        result = {
            "status": status,
            "review_id": candidate.review_id,
            "review_revision": candidate.revision + 1,
            "server_revision": server_revision,
            "operation_id": operation_id,
            "action": action,
        }
        self._record_operation(
            connection,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            candidate=candidate,
            action=action,
            result_code=status,
            result=result,
        )
        connection.execute(
            """
            UPDATE review_candidates
            SET status = %s, revision = revision + 1, resolved_at = now(),
                resolved_by = %s, last_operation_id = %s, updated_at = now()
            WHERE review_id = %s AND revision = %s
            """,
            (status, principal.user_id, operation_id, candidate.review_id, candidate.revision),
        )
        self._audit(connection, principal, candidate, f"review.{action}", status.upper(), result)
        return result

    def _revert_operation(
        self,
        connection: Any,
        candidate: ReviewCandidate,
        principal: Principal,
        original: tuple[Any, ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        original_id, action, backend_ref, target_ref = (str(original[0]), str(original[1]), original[2], original[3])
        operation_id = f"rvop_{uuid.uuid4().hex}"
        if action in {"confirm", "confirm_edit", "retain_both", "supersede"}:
            if not backend_ref:
                raise ReviewError("REVIEW_OPERATION_NOT_REVERSIBLE")
            if action == "supersede":
                if not target_ref:
                    raise ReviewError("REVIEW_OPERATION_NOT_REVERSIBLE")
                self._gbrain.restore_superseded(
                    idempotency_key=self._gbrain_key("restore", candidate.review_id, idempotency_key),
                    old_ref=str(target_ref),
                    new_ref=str(backend_ref),
                )
            else:
                self._gbrain.archive(
                    idempotency_key=self._gbrain_key("archive", candidate.review_id, idempotency_key),
                    reference=str(backend_ref),
                )
        elif action not in {"archive", "reject"}:
            raise ReviewError("REVIEW_OPERATION_NOT_REVERSIBLE")

        server_revision = self._next_revision(connection)
        result = {
            "status": "reverted",
            "review_id": candidate.review_id,
            "review_revision": candidate.revision + 1,
            "server_revision": server_revision,
            "operation_id": operation_id,
            "compensates_operation_id": original_id,
        }
        self._record_operation(
            connection,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            candidate=candidate,
            action="revert",
            result_code="reverted",
            result=result,
            backend_ref=str(backend_ref) if backend_ref else None,
            target_ref=str(target_ref) if target_ref else None,
            compensates_operation_id=original_id,
        )
        if backend_ref:
            connection.execute(
                """
                UPDATE memory_lifecycle
                SET status = 'archived', updated_server_revision = %s, updated_at = now()
                WHERE backend_ref = %s AND tenant_id = %s AND user_id = %s
                """,
                (server_revision, backend_ref, principal.tenant_id, principal.user_id),
            )
            self._insert_history(
                connection, str(backend_ref), principal, "compensate_archive", "active", "archived",
                server_revision, operation_id, str(target_ref) if target_ref else None,
            )
            self._insert_tombstone(
                connection, str(backend_ref), principal, server_revision, "archived"
            )
            self._mark_backend_crystal_stale(connection, str(backend_ref), server_revision)
        if action == "supersede" and target_ref:
            connection.execute(
                """
                UPDATE memory_lifecycle
                SET status = 'active', superseded_by = NULL, updated_server_revision = %s, updated_at = now()
                WHERE backend_ref = %s AND tenant_id = %s AND user_id = %s
                """,
                (server_revision, target_ref, principal.tenant_id, principal.user_id),
            )
            self._insert_history(
                connection, str(target_ref), principal, "compensate_restore", "superseded", "active",
                server_revision, operation_id, str(backend_ref),
            )
            self._revoke_tombstone(
                connection,
                str(target_ref),
                principal,
                server_revision,
            )
            self._mark_backend_crystal_stale(connection, str(target_ref), server_revision)
        connection.execute(
            """
            UPDATE review_candidates
            SET status = 'pending', revision = revision + 1, resolved_at = NULL,
                resolved_by = NULL, last_operation_id = %s, updated_at = now()
            WHERE review_id = %s AND revision = %s
            """,
            (operation_id, candidate.review_id, candidate.revision),
        )
        self._audit(connection, principal, candidate, "review.revert", "REVERTED", result)
        return result

    def _insert_confirmed_lifecycle(
        self,
        connection: Any,
        candidate: ReviewCandidate,
        backend_ref: str,
        resolved_payload: dict[str, Any],
        server_revision: int,
        operation_id: str,
    ) -> str:
        metadata = resolved_payload["metadata"]
        namespace_key = self._metadata_key(metadata.get("namespace_key"))
        if namespace_key is None:
            namespace_key = f"device:{candidate.origin.device_id}"
        binding_hash = scope_binding_hash(
            candidate.origin.tenant_id,
            candidate.origin.user_id,
            candidate.workspace_id,
            candidate.scope,
            namespace_key,
        )
        connection.execute(
            """
            INSERT INTO memory_lifecycle (
              backend_ref, tenant_id, user_id, workspace_id, scope,
              source_device_id, source_agent_installation_id, source_event_id, review_id,
              entity_key, attribute_key, temporal_key, namespace_key, scope_binding_hash,
              evidence, confidence, instruction_like, status,
              created_server_revision, updated_server_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      'user_confirmed', %s, %s, 'active', %s, %s)
            ON CONFLICT (backend_ref) DO NOTHING
            """,
            (
                backend_ref,
                candidate.origin.tenant_id,
                candidate.origin.user_id,
                candidate.workspace_id,
                candidate.scope,
                candidate.origin.device_id,
                candidate.origin.agent_installation_id,
                candidate.origin_event_id,
                candidate.review_id,
                self._metadata_key(metadata.get("entity_key")),
                self._metadata_key(metadata.get("attribute_key")),
                self._metadata_key(metadata.get("temporal_key")),
                namespace_key,
                binding_hash,
                float(resolved_payload["confidence"]),
                bool(resolved_payload["instruction_like"]),
                server_revision,
                server_revision,
            ),
        )
        self._insert_history(
            connection, backend_ref, candidate.origin, "review_confirmed", None, "active",
            server_revision, operation_id,
        )
        mark_crystal_stale(connection, binding_hash, server_revision)
        return binding_hash

    def _mark_superseded(
        self,
        connection: Any,
        candidate: ReviewCandidate,
        target_ref: str,
        backend_ref: str,
        server_revision: int,
        operation_id: str,
        binding_hash: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT status, pinned FROM memory_lifecycle
            WHERE backend_ref = %s AND tenant_id = %s AND user_id = %s
            FOR UPDATE
            """,
            (target_ref, candidate.origin.tenant_id, candidate.origin.user_id),
        ).fetchone()
        if row is None or str(row[0]) != "active" or bool(row[1]):
            raise ReviewError("SUPERSEDE_TARGET_INVALID")
        connection.execute(
            """
            UPDATE memory_lifecycle
            SET status = 'superseded', superseded_by = %s, updated_server_revision = %s, updated_at = now()
            WHERE backend_ref = %s
            """,
            (backend_ref, server_revision, target_ref),
        )
        self._insert_history(
            connection, target_ref, candidate.origin, "supersede", "active", "superseded",
            server_revision, operation_id, backend_ref,
        )
        self._insert_tombstone(
            connection, target_ref, candidate.origin, server_revision, "superseded"
        )
        mark_crystal_stale(connection, binding_hash, server_revision)

    def _find_conflicts(
        self,
        connection: Any,
        candidate: ReviewCandidate,
        resolved_payload: dict[str, Any],
        *,
        for_update: bool = False,
    ) -> list[dict[str, Any]]:
        metadata = resolved_payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        entity_key = self._metadata_key(metadata.get("entity_key"))
        attribute_key = self._metadata_key(metadata.get("attribute_key"))
        if entity_key is None or attribute_key is None:
            return []
        namespace_key = self._metadata_key(metadata.get("namespace_key"))
        if namespace_key is None:
            namespace_key = f"device:{candidate.origin.device_id}"
        temporal_key = self._metadata_key(metadata.get("temporal_key"))
        sql = """
            SELECT backend_ref, evidence, confidence, pinned, scope,
                   source_device_id, source_agent_installation_id
            FROM memory_lifecycle
            WHERE tenant_id = %s AND user_id = %s AND workspace_id = %s
              AND scope = %s AND namespace_key = %s
              AND entity_key = %s AND attribute_key = %s
              AND temporal_key IS NOT DISTINCT FROM %s
              AND status = 'active'
            ORDER BY updated_server_revision DESC
        """
        if for_update:
            sql += " FOR UPDATE"
        rows = connection.execute(
            sql,
            (
                candidate.origin.tenant_id,
                candidate.origin.user_id,
                candidate.workspace_id,
                candidate.scope,
                namespace_key,
                entity_key,
                attribute_key,
                temporal_key,
            ),
        ).fetchall()
        return [
            {
                "backend_ref": str(row[0]),
                "evidence": str(row[1]),
                "confidence": float(row[2]),
                "pinned": bool(row[3]),
                "scope": str(row[4]),
                "source_device_id": str(row[5]),
                "source_agent_installation_id": str(row[6]),
            }
            for row in rows
        ]

    def _resolved_payload(
        self, candidate: ReviewCandidate, request_payload: dict[str, Any], action: str
    ) -> dict[str, Any]:
        source = candidate.payload
        content = str(source.get("content") or "").strip()
        metadata = source.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        if action == "confirm_edit":
            content = self._required_text(request_payload.get("content"), "CONTENT_REQUIRED", 20_000)
            supplied_metadata = request_payload.get("metadata", metadata)
            if not isinstance(supplied_metadata, dict):
                raise ReviewError("METADATA_INVALID")
            metadata = dict(supplied_metadata)
        if not content:
            raise ReviewError("CONTENT_REQUIRED")
        try:
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ReviewError("METADATA_INVALID") from exc
        if len(metadata_json.encode("utf-8")) > 20_000:
            raise ReviewError("METADATA_TOO_LARGE")
        assessment = self._scanner.assess((content, metadata_json))
        if assessment.has_sensitive_content:
            raise ReviewError("SENSITIVE_CONTENT")
        instruction_like = bool(assessment.instruction_like)
        approved = bool(request_payload.get("approve_instruction_like"))
        if instruction_like and not approved:
            raise ReviewError("INSTRUCTION_REVIEW_CONFIRM_REQUIRED")
        try:
            confidence = float(source.get("confidence", 0.72))
        except (TypeError, ValueError) as exc:
            raise ReviewError("CONFIDENCE_INVALID") from exc
        if not 0 <= confidence <= 1:
            raise ReviewError("CONFIDENCE_INVALID")
        return {
            "content": content,
            "kind": str(source.get("kind") or "note"),
            "metadata": metadata,
            "confidence": confidence,
            "instruction_like": instruction_like,
            "approve_instruction_like": approved,
        }

    def _load_for_update(self, connection: Any, review_id: str) -> ReviewCandidate:
        row = connection.execute(
            """
            SELECT c.review_id, c.revision, c.status, c.expires_at, c.created_at,
                   e.device_id, e.event_id, e.tenant_id, e.user_id,
                   e.agent_installation_id, e.workspace_id, e.scope,
                   c.candidate_ciphertext, c.candidate_nonce, c.candidate_key_version,
                   c.last_operation_id
            FROM review_candidates AS c
            JOIN gateway_events AS e
              ON e.device_id = c.device_id AND e.event_id = c.event_id
            WHERE c.review_id = %s
            FOR UPDATE
            """,
            (review_id,),
        ).fetchone()
        if row is None:
            raise ReviewError("REVIEW_NOT_FOUND")
        return self._candidate_from_row(row)

    def _candidate_from_row(self, row: Any) -> ReviewCandidate:
        origin = Principal(
            tenant_id=str(row[7]),
            user_id=str(row[8]),
            device_id=str(row[5]),
            agent_installation_id=str(row[9]),
            workspace_ids=frozenset({str(row[10])}),
            capabilities=frozenset(),
        )
        try:
            encrypted = EncryptedPayload(bytes(row[12]), bytes(row[13]), str(row[14]))
            envelope = self._cipher.decrypt_json(
                encrypted,
                aad=(
                    f"{origin.tenant_id}:{origin.user_id}:{origin.device_id}:"
                    f"{origin.agent_installation_id}:{row[6]}"
                ).encode("utf-8"),
            )
        except EncryptionError as exc:
            raise ReviewError("REVIEW_CANDIDATE_UNREADABLE") from exc
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise ReviewError("REVIEW_CANDIDATE_UNREADABLE")
        candidate = ReviewCandidate(
            review_id=str(row[0]),
            revision=int(row[1]),
            status=str(row[2]),
            expires_at=row[3],
            created_at=row[4],
            origin=origin,
            origin_event_id=str(row[6]),
            workspace_id=str(row[10]),
            scope=str(row[11]),
            last_operation_id=str(row[15]) if row[15] is not None else None,
            payload=payload,
        )
        return candidate

    @staticmethod
    def _require_review_binding(connection: Any, principal: Principal, workspace_id: str) -> None:
        PostgresEventLedger._require_binding(connection, principal, workspace_id, "memory.manage")

    @staticmethod
    def _require_candidate_owner(candidate: ReviewCandidate, principal: Principal, workspace_id: str) -> None:
        if (
            candidate.origin.tenant_id != principal.tenant_id
            or candidate.origin.user_id != principal.user_id
            or candidate.workspace_id != workspace_id
        ):
            raise ReviewError("REVIEW_FORBIDDEN")

    @staticmethod
    def _require_expected_revision(candidate: ReviewCandidate, expected_revision: int) -> None:
        if candidate.revision != expected_revision:
            raise ReviewError("REVIEW_REVISION_CONFLICT")

    def _existing_operation(
        self, connection: Any, idempotency_key: str, principal: Principal, workspace_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT operation_id, result_json
            FROM review_operations
            WHERE idempotency_key = %s AND tenant_id = %s AND user_id = %s AND workspace_id = %s
            """,
            (idempotency_key, principal.tenant_id, principal.user_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        result = row[1]
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError as exc:
                raise ReviewError("REVIEW_OPERATION_CORRUPT") from exc
        if not isinstance(result, dict):
            raise ReviewError("REVIEW_OPERATION_CORRUPT")
        return dict(result)

    def _validate_supersede_target(
        self, payload: dict[str, Any], conflicts: list[dict[str, Any]]
    ) -> None:
        target_ref = self._required_text(payload.get("target_ref"), "SUPERSEDE_TARGET_REQUIRED", 128)
        if target_ref not in {item["backend_ref"] for item in conflicts}:
            raise ReviewError("SUPERSEDE_TARGET_INVALID")
        if any(item["backend_ref"] == target_ref and item["pinned"] for item in conflicts):
            raise ReviewError("SUPERSEDE_TARGET_PINNED")

    @staticmethod
    def _suggest_action(resolved_payload: dict[str, Any], conflicts: list[dict[str, Any]]) -> str:
        candidate_rank = EVIDENCE_RANK.get("user_confirmed", 0)
        if any(item["pinned"] for item in conflicts):
            return "retain_both"
        if all(candidate_rank > EVIDENCE_RANK.get(item["evidence"], 0) for item in conflicts):
            return "supersede"
        return "retain_both"

    @staticmethod
    def _next_revision(connection: Any) -> int:
        connection.execute(
            """
            INSERT INTO gateway_state (state_key, state_value)
            VALUES ('server_revision', '0')
            ON CONFLICT (state_key) DO NOTHING
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

    def _record_operation(
        self,
        connection: Any,
        *,
        operation_id: str,
        idempotency_key: str,
        candidate: ReviewCandidate,
        action: str,
        result_code: str,
        result: dict[str, Any],
        backend_ref: str | None = None,
        target_ref: str | None = None,
        content_hash: str | None = None,
        compensates_operation_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO review_operations (
              operation_id, idempotency_key, review_id, tenant_id, user_id, workspace_id,
              action, expected_revision, result_code, backend_ref, target_ref, content_hash,
              compensates_operation_id, result_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                operation_id, idempotency_key, candidate.review_id,
                candidate.origin.tenant_id, candidate.origin.user_id, candidate.workspace_id,
                action, candidate.revision, result_code, backend_ref, target_ref, content_hash,
                compensates_operation_id,
                json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )

    def _insert_history(
        self,
        connection: Any,
        backend_ref: str,
        principal: Principal,
        action: str,
        from_status: str | None,
        to_status: str,
        server_revision: int,
        operation_id: str | None,
        related_ref: str | None = None,
    ) -> None:
        history_id = "hist_" + hashlib.sha256(
            f"{backend_ref}:{operation_id or action}:{server_revision}".encode("utf-8")
        ).hexdigest()[:40]
        connection.execute(
            """
            INSERT INTO memory_lifecycle_history (
              history_id, backend_ref, operation_id, tenant_id, user_id, action,
              from_status, to_status, related_ref, server_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (history_id) DO NOTHING
            """,
            (
                history_id, backend_ref, operation_id, principal.tenant_id, principal.user_id,
                action, from_status, to_status, related_ref, server_revision,
            ),
        )

    def _insert_tombstone(
        self,
        connection: Any,
        backend_ref: str,
        principal: Principal,
        server_revision: int,
        reason_code: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_tombstones (
              memory_id, tenant_id, user_id, backend_ref, deleted_revision,
              deleted_by_device_id, reason_code, revoked_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
            ON CONFLICT (backend_ref) DO UPDATE SET
              deleted_revision = EXCLUDED.deleted_revision,
              deleted_at = now(),
              deleted_by_device_id = EXCLUDED.deleted_by_device_id,
              reason_code = EXCLUDED.reason_code,
              revoked_revision = NULL
            """,
            (
                backend_ref, principal.tenant_id, principal.user_id, backend_ref,
                server_revision, principal.device_id, reason_code,
            ),
        )

    @staticmethod
    def _revoke_tombstone(
        connection: Any,
        backend_ref: str,
        principal: Principal,
        server_revision: int,
    ) -> None:
        """撤销一次可见删除，而不物理删除审计记录。"""

        changed = connection.execute(
            """
            UPDATE memory_tombstones
            SET revoked_revision = %s
            WHERE backend_ref = %s
              AND tenant_id = %s
              AND user_id = %s
              AND revoked_revision IS NULL
            """,
            (server_revision, backend_ref, principal.tenant_id, principal.user_id),
        ).rowcount
        if changed != 1:
            raise ReviewError("SUPERSEDE_TOMBSTONE_MISSING")

    @staticmethod
    def _mark_backend_crystal_stale(connection: Any, backend_ref: str, server_revision: int) -> None:
        row = connection.execute(
            "SELECT scope_binding_hash FROM memory_lifecycle WHERE backend_ref = %s",
            (backend_ref,),
        ).fetchone()
        if row is not None and row[0] is not None:
            mark_crystal_stale(connection, str(row[0]), server_revision)

    def _audit(
        self,
        connection: Any,
        principal: Principal,
        candidate: ReviewCandidate,
        action: str,
        result_code: str,
        details: dict[str, Any],
    ) -> None:
        safe_details = {
            key: value for key, value in details.items()
            if key not in {"content", "metadata"}
        }
        connection.execute(
            """
            INSERT INTO audit_log (
              tenant_id, actor_type, actor_id, action, result_code, trace_id,
              device_id, agent_installation_id, workspace_id, target_ref, details_json
            ) VALUES (%s, 'device', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                principal.tenant_id, principal.agent_installation_id, action, result_code,
                f"tr_{uuid.uuid4().hex}", principal.device_id, principal.agent_installation_id,
                candidate.workspace_id, candidate.review_id,
                json.dumps(safe_details, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )

    def _present_candidate(
        self, candidate: ReviewCandidate, conflicts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = candidate.payload
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return {
            "review_id": candidate.review_id,
            "revision": candidate.revision,
            "status": candidate.status,
            "expires_at": candidate.expires_at.isoformat() if hasattr(candidate.expires_at, "isoformat") else candidate.expires_at,
            "created_at": candidate.created_at.isoformat() if hasattr(candidate.created_at, "isoformat") else candidate.created_at,
            "content": str(payload.get("content") or ""),
            "kind": str(payload.get("kind") or "note"),
            "scope": candidate.scope,
            "metadata": metadata,
            "evidence": str(payload.get("evidence") or ""),
            "confidence": float(payload.get("confidence", 0.72)),
            "instruction_like": bool(payload.get("instruction_like")),
            "instruction_rule_ids": list(payload.get("instruction_rule_ids") or []),
            "source": {
                "device_id": candidate.origin.device_id,
                "agent_installation_id": candidate.origin.agent_installation_id,
                "workspace_id": candidate.workspace_id,
                "event_id": candidate.origin_event_id,
            },
            "suggested_action": self._suggest_action(payload, conflicts) if conflicts else "confirm",
            "conflicts": conflicts,
        }

    @staticmethod
    def _metadata_key(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized[:256] or None

    @staticmethod
    def _content_hash(payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            {"content": payload["content"], "metadata": payload["metadata"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _metadata_binding_key(candidate: ReviewCandidate, operation_id: str) -> str:
        return f"review:{candidate.origin.device_id}:{candidate.origin_event_id}:{operation_id}"

    @staticmethod
    def _gbrain_key(kind: str, review_id: str, idempotency_key: str) -> str:
        digest = hashlib.sha256(f"{kind}:{review_id}:{idempotency_key}".encode("utf-8")).hexdigest()
        return f"review-{kind}-{digest}"

    @staticmethod
    def _required_text(value: Any, code: str, maximum: int) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > maximum:
            raise ReviewError(code)
        return normalized

    @staticmethod
    def _positive_int(value: Any, code: str) -> int:
        if isinstance(value, bool):
            raise ReviewError(code)
        try:
            converted = int(value)
        except (TypeError, ValueError) as exc:
            raise ReviewError(code) from exc
        if converted <= 0:
            raise ReviewError(code)
        return converted

    @classmethod
    def _bounded_int(cls, value: Any, code: str, minimum: int, maximum: int) -> int:
        converted = cls._positive_int(value, code)
        if not minimum <= converted <= maximum:
            raise ReviewError(code)
        return converted
