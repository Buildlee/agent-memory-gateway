"""Gateway pending 事件到 GBrain 的可恢复对账 worker。"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from .auth import Principal
from .crypto import EncryptedPayload, EncryptionError, EventCipher
from .crystal_service import mark_crystal_stale, scope_binding_hash
from .db_pool import PostgresConnectionPool
from .gbrain_backend import GBrainBackend, GBrainBackendError, GBrainSecurityError
from .metadata_store import MetadataStoreError
from .metadata_migrations import MigrationError, inspect_metadata_schema


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    event_id: str | None = None
    backend_ref: str | None = None
    server_revision: int | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status}
        if self.event_id is not None:
            result["event_id"] = self.event_id
        if self.backend_ref is not None:
            result["backend_ref"] = self.backend_ref
        if self.server_revision is not None:
            result["server_revision"] = self.server_revision
        return result


def reconcile_cycle(worker: "PendingEventWorker", *, once: bool, limit: int) -> list[ReconcileResult]:
    """执行一个有限对账周期，供一次性命令和常驻 worker 共用。"""

    return [worker.reconcile_once()] if once else worker.reconcile(limit)


class PendingEventWorker:
    """锁定一条 pending 事件，在跨库写入后记录固定回执。"""

    def __init__(
        self,
        metadata_dsn: str,
        cipher: EventCipher,
        gbrain: GBrainBackend,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not metadata_dsn:
            raise MetadataStoreError("缺少元数据库运行连接串")
        self._metadata_dsn = metadata_dsn
        self._cipher = cipher
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

    def reconcile_once(self) -> ReconcileResult:
        """最多处理一条事件；无工作时不写入数据库。"""

        with self._connect() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT device_id, event_id, tenant_id, user_id, agent_installation_id,
                           workspace_id, payload_ciphertext, payload_nonce, payload_key_version,
                           instruction_like, retry_count
                    FROM gateway_events
                    WHERE status IN ('pending', 'retryable_failed')
                      AND (next_retry_at IS NULL OR next_retry_at <= now())
                    ORDER BY received_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return ReconcileResult(status="idle")

                event_id = str(row[1])
                trace_id = f"tr_{uuid.uuid4().hex}"
                principal = Principal(
                    tenant_id=str(row[2]),
                    user_id=str(row[3]),
                    device_id=str(row[0]),
                    agent_installation_id=str(row[4]),
                    workspace_ids=frozenset({str(row[5])}),
                    capabilities=frozenset({"memory.write_event"}),
                )
                encrypted = EncryptedPayload(bytes(row[6]), bytes(row[7]), str(row[8]))
                try:
                    envelope = self._cipher.decrypt_json(
                        encrypted,
                        aad=(
                            f"{principal.tenant_id}:{principal.user_id}:{principal.device_id}:"
                            f"{principal.agent_installation_id}:{event_id}"
                        ).encode("utf-8"),
                    )
                    event_payload = envelope.get("payload")
                    if not isinstance(event_payload, dict):
                        raise EncryptionError("待处理事件缺少 payload")
                    provenance = self._external_provenance(event_payload)
                    if provenance is not None:
                        duplicate_ref = self._existing_external_binding(connection, principal, provenance)
                        if duplicate_ref is not None:
                            return self._complete_source_duplicate(
                                connection,
                                principal,
                                event_id,
                                trace_id,
                                duplicate_ref or None,
                            )
                    if bool(row[9]) or str(event_payload.get("evidence") or "") != "user_explicit":
                        return self._create_review_candidate(
                            connection,
                            principal,
                            event_id,
                            encrypted,
                            trace_id,
                            provenance,
                        )
                    backend_ref = self._gbrain.upsert_confirmed(
                        idempotency_key=f"{principal.device_id}:{event_id}",
                        tenant_id=principal.tenant_id,
                        content=str(event_payload.get("content") or ""),
                        kind=str(event_payload.get("kind") or "note"),
                        confidence=float(event_payload.get("confidence", 0.72)),
                    )
                except EncryptionError:
                    self._mark_dead_letter(connection, principal, event_id, trace_id, "EVENT_DECRYPT_FAILED")
                    return ReconcileResult(status="dead_letter", event_id=event_id)
                except GBrainSecurityError:
                    self._mark_dead_letter(connection, principal, event_id, trace_id, "GBRAIN_SECURITY_REJECTED")
                    return ReconcileResult(status="dead_letter", event_id=event_id)
                except (GBrainBackendError, OSError, TimeoutError):
                    self._mark_retryable(connection, principal, event_id, int(row[10]), trace_id)
                    return ReconcileResult(status="retryable_failed", event_id=event_id)

                revision = self._next_revision(connection)
                ack_id = f"ack_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    UPDATE gateway_events
                    SET status = 'applied', result_code = 'candidate_confirmed',
                        error_code = NULL, error_retryable = NULL, backend_ref = %s,
                        server_revision = %s, processed_at = now(), next_retry_at = NULL
                    WHERE device_id = %s AND event_id = %s
                    """,
                    (backend_ref, revision, principal.device_id, event_id),
                )
                connection.execute(
                    """
                    INSERT INTO backend_bindings (
                      idempotency_key, device_id, event_id, backend_name, backend_ref, payload_hash
                    )
                    SELECT %s, device_id, event_id, 'gbrain', %s, payload_hash
                    FROM gateway_events
                    WHERE device_id = %s AND event_id = %s
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (f"{principal.device_id}:{event_id}", backend_ref, principal.device_id, event_id),
                )
                self._register_active_lifecycle(
                    connection,
                    principal,
                    event_id,
                    event_payload,
                    backend_ref,
                    revision,
                )
                self._register_external_binding(
                    connection,
                    principal,
                    event_id,
                    provenance,
                    backend_ref,
                )
                connection.execute(
                    """
                    INSERT INTO event_receipts (
                      device_id, event_id, ack_id, status, result_code, backend_ref,
                      server_revision, trace_id, processed_at
                    ) VALUES (%s, %s, %s, 'applied', 'candidate_confirmed', %s, %s, %s, now())
                    ON CONFLICT (device_id, event_id) DO NOTHING
                    """,
                    (principal.device_id, event_id, ack_id, backend_ref, revision, trace_id),
                )
                self._audit(connection, principal, event_id, trace_id, "event.applied", "APPLIED")
                return ReconcileResult(
                    status="applied", event_id=event_id, backend_ref=backend_ref, server_revision=revision
                )

    def _create_review_candidate(
        self,
        connection: Any,
        principal: Principal,
        event_id: str,
        encrypted: EncryptedPayload,
        trace_id: str,
        provenance: dict[str, str] | None = None,
    ) -> ReconcileResult:
        """普通 Agent 观察只进入审核，不直接变成 GBrain 长期事实。"""

        review_id = f"review_{uuid.uuid4().hex}"
        revision = self._next_revision(connection)
        connection.execute(
            """
            INSERT INTO review_candidates (
              review_id, device_id, event_id, candidate_ciphertext, candidate_nonce,
              candidate_key_version, status, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', now() + interval '30 days')
            """,
            (
                review_id,
                principal.device_id,
                event_id,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.key_version,
            ),
        )
        connection.execute(
            """
            UPDATE gateway_events
            SET status = 'applied', result_code = 'candidate_created',
                server_revision = %s, processed_at = now(), next_retry_at = NULL
            WHERE device_id = %s AND event_id = %s
            """,
            (revision, principal.device_id, event_id),
        )
        connection.execute(
            """
            INSERT INTO event_receipts (
              device_id, event_id, ack_id, status, result_code, server_revision, trace_id, processed_at
            ) VALUES (%s, %s, %s, 'applied', 'candidate_created', %s, %s, now())
            ON CONFLICT (device_id, event_id) DO NOTHING
            """,
            (principal.device_id, event_id, f"ack_{uuid.uuid4().hex}", revision, trace_id),
        )
        self._register_external_binding(
            connection,
            principal,
            event_id,
            provenance,
            None,
        )
        self._audit(connection, principal, event_id, trace_id, "review.created", "CANDIDATE_CREATED")
        return ReconcileResult(status="applied", event_id=event_id, server_revision=revision)

    @staticmethod
    def _external_provenance(event_payload: dict[str, Any]) -> dict[str, str] | None:
        """只接受端侧适配器的最小来源指纹，不保存文件路径或记忆正文。"""

        metadata = event_payload.get("metadata")
        raw = metadata.get("provenance") if isinstance(metadata, dict) else None
        if not isinstance(raw, dict):
            return None
        values = {
            "provider_type": str(raw.get("provider_type") or "").strip(),
            "provider_instance_id": str(raw.get("provider_instance_id") or "").strip(),
            "source_record_id": str(raw.get("source_record_id") or "").strip(),
            "source_revision": str(raw.get("source_revision") or "").strip().lower(),
            "capture_mode": str(raw.get("capture_mode") or "").strip(),
        }
        identifier = re.compile(r"[A-Za-z0-9_.@:-]{1,128}\Z")
        if not all(identifier.fullmatch(values[key]) for key in (
            "provider_type", "provider_instance_id", "source_record_id"
        )):
            return None
        if re.fullmatch(r"[0-9a-f]{64}", values["source_revision"]) is None:
            return None
        if values["capture_mode"] not in {"manual_selection", "automatic_whitelist"}:
            return None
        return values

    @staticmethod
    def _existing_external_binding(
        connection: Any,
        principal: Principal,
        provenance: dict[str, str],
    ) -> str | None:
        row = connection.execute(
            """
            SELECT COALESCE(backend_ref, '')
            FROM external_memory_bindings
            WHERE tenant_id = %s AND user_id = %s
              AND workspace_id = %s
              AND provider_instance_id = %s AND source_record_id = %s
              AND source_revision = %s
            FOR UPDATE
            """,
            (
                principal.tenant_id,
                principal.user_id,
                next(iter(principal.workspace_ids)),
                provenance["provider_instance_id"],
                provenance["source_record_id"],
                provenance["source_revision"],
            ),
        ).fetchone()
        return None if row is None else str(row[0] or "")

    @staticmethod
    def _register_external_binding(
        connection: Any,
        principal: Principal,
        event_id: str,
        provenance: dict[str, str] | None,
        backend_ref: str | None,
    ) -> None:
        if provenance is None:
            return
        connection.execute(
            """
            INSERT INTO external_memory_bindings (
              tenant_id, user_id, device_id, agent_installation_id, workspace_id,
              provider_type, provider_instance_id, source_record_id, source_revision,
              capture_mode, event_id, backend_ref
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, user_id, workspace_id, provider_instance_id, source_record_id, source_revision)
            DO UPDATE SET backend_ref = COALESCE(EXCLUDED.backend_ref, external_memory_bindings.backend_ref),
                          updated_at = now()
            """,
            (
                principal.tenant_id,
                principal.user_id,
                principal.device_id,
                principal.agent_installation_id,
                next(iter(principal.workspace_ids)),
                provenance["provider_type"],
                provenance["provider_instance_id"],
                provenance["source_record_id"],
                provenance["source_revision"],
                provenance["capture_mode"],
                event_id,
                backend_ref,
            ),
        )

    def _complete_source_duplicate(
        self,
        connection: Any,
        principal: Principal,
        event_id: str,
        trace_id: str,
        backend_ref: str | None,
    ) -> ReconcileResult:
        revision = self._next_revision(connection)
        connection.execute(
            """
            UPDATE gateway_events
            SET status = 'applied', result_code = 'source_duplicate',
                error_code = NULL, error_retryable = NULL, backend_ref = %s,
                server_revision = %s, processed_at = now(), next_retry_at = NULL
            WHERE device_id = %s AND event_id = %s
            """,
            (backend_ref, revision, principal.device_id, event_id),
        )
        connection.execute(
            """
            INSERT INTO event_receipts (
              device_id, event_id, ack_id, status, result_code, backend_ref,
              server_revision, trace_id, processed_at
            ) VALUES (%s, %s, %s, 'applied', 'source_duplicate', %s, %s, %s, now())
            ON CONFLICT (device_id, event_id) DO NOTHING
            """,
            (
                principal.device_id,
                event_id,
                f"ack_{uuid.uuid4().hex}",
                backend_ref,
                revision,
                trace_id,
            ),
        )
        self._audit(connection, principal, event_id, trace_id, "event.deduplicated", "SOURCE_DUPLICATE")
        return ReconcileResult(
            status="applied",
            event_id=event_id,
            backend_ref=backend_ref,
            server_revision=revision,
        )

    @staticmethod
    def _register_active_lifecycle(
        connection: Any,
        principal: Principal,
        event_id: str,
        event_payload: dict[str, Any],
        backend_ref: str,
        revision: int,
    ) -> None:
        """为自动确认事实追加生命周期索引；正文始终不进入元数据明文列。"""

        metadata = event_payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        entity_key = PendingEventWorker._optional_metadata_key(metadata.get("entity_key"))
        attribute_key = PendingEventWorker._optional_metadata_key(metadata.get("attribute_key"))
        temporal_key = PendingEventWorker._optional_metadata_key(metadata.get("temporal_key"))
        namespace_key = PendingEventWorker._optional_metadata_key(metadata.get("namespace_key"))
        if namespace_key is None:
            namespace_key = f"device:{principal.device_id}"
        scope = str(event_payload.get("requested_scope") or "workspace")
        workspace_id = next(iter(principal.workspace_ids))
        binding_hash = scope_binding_hash(
            principal.tenant_id,
            principal.user_id,
            workspace_id,
            scope,
            namespace_key,
        )
        evidence = str(event_payload.get("evidence") or "user_explicit")[:128]
        confidence = float(event_payload.get("confidence", 0.72))
        connection.execute(
            """
            INSERT INTO memory_lifecycle (
              backend_ref, tenant_id, user_id, workspace_id, scope,
              source_device_id, source_agent_installation_id, source_event_id,
              entity_key, attribute_key, temporal_key, namespace_key, scope_binding_hash,
              evidence, confidence, instruction_like,
              status, created_server_revision, updated_server_revision
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false,
                      'active', %s, %s)
            ON CONFLICT (backend_ref) DO NOTHING
            """,
            (
                backend_ref,
                principal.tenant_id,
                principal.user_id,
                workspace_id,
                scope,
                principal.device_id,
                principal.agent_installation_id,
                event_id,
                entity_key,
                attribute_key,
                temporal_key,
                namespace_key,
                binding_hash,
                evidence,
                confidence,
                revision,
                revision,
            ),
        )
        history_id = "hist_auto_" + hashlib.sha256(
            f"{backend_ref}:{revision}".encode("utf-8")
        ).hexdigest()[:32]
        connection.execute(
            """
            INSERT INTO memory_lifecycle_history (
              history_id, backend_ref, tenant_id, user_id, action, to_status, server_revision
            ) VALUES (%s, %s, %s, %s, 'auto_confirmed', 'active', %s)
            ON CONFLICT (history_id) DO NOTHING
            """,
            (history_id, backend_ref, principal.tenant_id, principal.user_id, revision),
        )
        mark_crystal_stale(connection, binding_hash, revision)

    @staticmethod
    def _optional_metadata_key(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized[:256] or None

    def reconcile(self, limit: int) -> list[ReconcileResult]:
        bounded_limit = max(1, min(int(limit), 1000))
        results: list[ReconcileResult] = []
        for _ in range(bounded_limit):
            result = self.reconcile_once()
            results.append(result)
            if result.status == "idle":
                break
        return results

    def record_heartbeat(self) -> None:
        """写入不含事件正文的 worker 心跳，供 Gateway readiness 使用。"""

        value = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO gateway_state (state_key, state_value, updated_at)
                    VALUES ('worker_heartbeat', %s, now())
                    ON CONFLICT (state_key) DO UPDATE
                    SET state_value = EXCLUDED.state_value, updated_at = now()
                    """,
                    (value,),
                )

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

    @staticmethod
    def _audit(
        connection: Any,
        principal: Principal,
        event_id: str,
        trace_id: str,
        action: str,
        result_code: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_log (
              tenant_id, actor_type, actor_id, action, result_code, trace_id,
              device_id, agent_installation_id, workspace_id, target_ref
            ) VALUES (%s, 'system', 'reconcile-worker', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                principal.tenant_id,
                action,
                result_code,
                trace_id,
                principal.device_id,
                principal.agent_installation_id,
                next(iter(principal.workspace_ids)),
                event_id,
            ),
        )

    def _mark_retryable(
        self, connection: Any, principal: Principal, event_id: str, retry_count: int, trace_id: str
    ) -> None:
        delay_seconds = min(300, 2 ** min(retry_count + 1, 8))
        next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        connection.execute(
            """
            UPDATE gateway_events
            SET status = 'retryable_failed', error_code = 'GBRAIN_UNAVAILABLE',
                error_retryable = true, retry_count = retry_count + 1, next_retry_at = %s
            WHERE device_id = %s AND event_id = %s
            """,
            (next_retry_at, principal.device_id, event_id),
        )
        self._audit(connection, principal, event_id, trace_id, "event.retry", "GBRAIN_UNAVAILABLE")

    def _mark_dead_letter(
        self, connection: Any, principal: Principal, event_id: str, trace_id: str, error_code: str
    ) -> None:
        connection.execute(
            """
            UPDATE gateway_events
            SET status = 'dead_letter', error_code = %s, error_retryable = false,
                processed_at = now()
            WHERE device_id = %s AND event_id = %s
            """,
            (error_code, principal.device_id, event_id),
        )
        connection.execute(
            """
            INSERT INTO dead_letters (dead_letter_id, device_id, event_id, error_code, last_error_class)
            VALUES (%s, %s, %s, %s, 'encryption')
            ON CONFLICT (device_id, event_id) DO NOTHING
            """,
            (f"dl_{uuid.uuid4().hex}", principal.device_id, event_id, error_code),
        )
        self._audit(connection, principal, event_id, trace_id, "event.dead_letter", error_code)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="对账 Gateway pending 事件到 GBrain")
    parser.add_argument("--metadata-dsn", default=os.environ.get("MEMORY_METADATA_RUNTIME_DSN"))
    parser.add_argument("--gbrain-dsn", default=os.environ.get("MEMORY_GBRAIN_BACKEND_DSN"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--once", action="store_true", help="最多处理一条事件")
    parser.add_argument("--forever", action="store_true", help="作为常驻 worker 持续轮询待处理事件")
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=5.0,
        help="常驻 worker 两次轮询之间的等待时间，默认 5 秒",
    )
    args = parser.parse_args(argv)
    if not args.metadata_dsn or not args.gbrain_dsn:
        parser.error("需要元数据库和 GBrain 后端连接串")
    if args.once and args.forever:
        parser.error("--once 不能与 --forever 同时使用")
    if not 0.1 <= args.poll_interval_seconds <= 300:
        parser.error("--poll-interval-seconds 必须在 0.1 到 300 之间")
    metadata_pool = PostgresConnectionPool.from_environment(
        args.metadata_dsn,
        name="memory-worker-metadata",
        environment_prefix="MEMORY_WORKER_METADATA_POOL",
        default_max_size=2,
    )
    gbrain_pool = PostgresConnectionPool.from_environment(
        args.gbrain_dsn,
        name="memory-worker-gbrain",
        environment_prefix="MEMORY_WORKER_GBRAIN_POOL",
        default_max_size=2,
    )
    try:
        metadata_pool.wait()
        gbrain_pool.wait()
        try:
            metadata_report = inspect_metadata_schema(args.metadata_dsn)
        except MigrationError as exc:
            parser.error(str(exc))
        if not metadata_report.compatible:
            parser.error("元数据库迁移不完整或校验值不一致，请先执行 memory-gateway migrate --verify")
        gbrain = GBrainBackend(args.gbrain_dsn, connection_factory=gbrain_pool.connection)
        gbrain.schema_version()
        worker = PendingEventWorker(
            args.metadata_dsn,
            EventCipher.from_environment(),
            gbrain,
            connection_factory=metadata_pool.connection,
        )
        if args.forever:
            while True:
                worker.record_heartbeat()
                for result in reconcile_cycle(worker, once=False, limit=args.limit):
                    if result.status != "idle":
                        print(result.as_dict(), flush=True)
                worker.record_heartbeat()
                time.sleep(args.poll_interval_seconds)
        else:
            for result in reconcile_cycle(worker, once=args.once, limit=args.limit):
                print(result.as_dict())
    except KeyboardInterrupt:
        pass
    finally:
        gbrain_pool.close()
        metadata_pool.close()
