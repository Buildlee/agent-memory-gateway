"""Gateway push/pull 同步协议：独立回执、授权增量、游标和墓碑。"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Callable

from .auth import AuthError, Principal, TokenAuthenticator
from .crypto import EncryptedPayload, EncryptionError, EventCipher
from .db_pool import DatabasePoolBusy
from .event_contract import EventValidationError
from .metadata_store import MetadataStoreError, PostgresEventLedger


SYNC_PROTOCOL_VERSION = 1
SYNC_POLICY_VERSION = "2026-07-12.2"
MAX_PUSH_EVENTS = 100
MAX_PUSH_BYTES = 1_048_576
MAX_PULL_LIMIT = 100


class SyncProtocolError(ValueError):
    """可安全返回给 Sidecar 的稳定同步错误。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _required_text(payload: dict[str, Any], field: str, maximum: int) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise SyncProtocolError(f"{field.upper()}_REQUIRED")
    if len(value) > maximum:
        raise SyncProtocolError(f"{field.upper()}_TOO_LONG")
    return value


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise SyncProtocolError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SyncProtocolError(code) from exc
    if parsed < minimum:
        raise SyncProtocolError(code)
    return parsed


class PostgresSyncService:
    """每个事件独立提交；pull 只返回数据库已授权的 revision。"""

    def __init__(
        self,
        metadata_dsn: str,
        ledger: PostgresEventLedger,
        cipher: EventCipher,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not metadata_dsn:
            raise MetadataStoreError("缺少元数据库运行连接串")
        self._metadata_dsn = metadata_dsn
        self._ledger = ledger
        self._cipher = cipher
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

    def push(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        batch_id = _required_text(payload, "batch_id", 128)
        workspace_id = _required_text(payload, "workspace_id", 256)
        principal.require_workspace(workspace_id)
        if str(payload.get("device_id") or "") != principal.device_id:
            raise AuthError("IDENTITY_MISMATCH")
        if _integer(payload.get("protocol_version"), "PROTOCOL_VERSION_UNSUPPORTED", minimum=1) != 1:
            raise SyncProtocolError("PROTOCOL_VERSION_UNSUPPORTED")
        try:
            encoded_size = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise SyncProtocolError("BATCH_INVALID") from exc
        if encoded_size > MAX_PUSH_BYTES:
            raise SyncProtocolError("BATCH_TOO_LARGE")
        events = payload.get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_PUSH_EVENTS:
            raise SyncProtocolError("BATCH_EVENT_COUNT_INVALID")
        sequences: list[int] = []
        for event in events:
            if not isinstance(event, dict):
                raise SyncProtocolError("BATCH_EVENT_INVALID")
            sequences.append(_integer(event.get("device_seq"), "DEVICE_SEQ_INVALID"))
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise SyncProtocolError("BATCH_NOT_ORDERED")

        current_epoch = self._read_sync_epoch()
        supplied_epoch = str(payload.get("sync_epoch") or "").strip()
        if supplied_epoch and supplied_epoch != current_epoch:
            return self._epoch_reset_response(current_epoch)

        results: list[dict[str, Any]] = []
        for event in events:
            event_id = str(event.get("event_id") or "evt_unidentified")[:128]
            try:
                TokenAuthenticator.validate_payload_identity(principal, event)
                if str(event.get("workspace_id") or "") != workspace_id:
                    raise AuthError("WORKSPACE_FORBIDDEN")
                results.append(self._ledger.record_proposed_event(event, principal))
            except EventValidationError as exc:
                results.append(
                    {
                        "event_id": event_id,
                        "status": "rejected",
                        "error": exc.code,
                        "retryable": False,
                    }
                )
            except AuthError as exc:
                results.append(
                    {
                        "event_id": event_id,
                        "status": "rejected",
                        "error": exc.code,
                        "retryable": False,
                    }
                )
            except DatabasePoolBusy as exc:
                results.append(
                    {
                        "event_id": event_id,
                        "status": "retryable_failed",
                        "error": exc.code,
                        "retryable": True,
                    }
                )
        missing = self._advance_contiguous_sequence(principal.device_id)
        return {
            "batch_id": batch_id,
            "sync_epoch": current_epoch,
            "results": results,
            "missing_device_seq": missing,
            "trace_id": f"tr_{uuid.uuid4().hex}",
        }

    def pull(self, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        workspace_id = _required_text(payload, "workspace_id", 256)
        principal.require_workspace(workspace_id)
        if _integer(payload.get("protocol_version"), "PROTOCOL_VERSION_UNSUPPORTED", minimum=1) != 1:
            raise SyncProtocolError("PROTOCOL_VERSION_UNSUPPORTED")
        last_seen = _integer(payload.get("last_seen_revision", 0), "LAST_SEEN_REVISION_INVALID")
        limit = min(
            _integer(payload.get("limit", 50), "LIMIT_INVALID", minimum=1),
            MAX_PULL_LIMIT,
        )
        current_epoch = self._read_sync_epoch()
        supplied_epoch = str(payload.get("sync_epoch") or "").strip()
        if supplied_epoch and supplied_epoch != current_epoch:
            return self._epoch_reset_response(current_epoch)
        cursor = str(payload.get("cursor") or "").strip()
        start_revision = self._decode_cursor(cursor, current_epoch, workspace_id) if cursor else last_seen
        if start_revision < last_seen:
            raise SyncProtocolError("CURSOR_INVALID")

        with self._connect() as connection:
            with connection.transaction():
                PostgresEventLedger._require_binding(
                    connection, principal, workspace_id, "memory.read_context"
                )
                self._record_checkpoint(
                    connection,
                    principal,
                    workspace_id,
                    last_seen,
                    current_epoch,
                )
                event_rows = connection.execute(
                    """
                    SELECT device_id, event_id, agent_installation_id, workspace_id,
                           scope, backend_ref, server_revision, payload_ciphertext,
                           payload_nonce, payload_key_version
                    FROM gateway_events
                    WHERE tenant_id = %s
                      AND user_id = %s
                      AND status = 'applied'
                      AND backend_ref IS NOT NULL
                      AND instruction_like = false
                      AND server_revision > %s
                      AND (
                        scope = 'user'
                        OR (scope = 'workspace' AND workspace_id = %s)
                        OR (scope = 'device' AND device_id = %s)
                        OR (scope = 'agent' AND agent_installation_id = %s)
                        OR (scope = 'private' AND device_id = %s AND agent_installation_id = %s)
                      )
                    ORDER BY server_revision ASC
                    LIMIT %s
                    """,
                    (
                        principal.tenant_id,
                        principal.user_id,
                        start_revision,
                        workspace_id,
                        principal.device_id,
                        principal.agent_installation_id,
                        principal.device_id,
                        principal.agent_installation_id,
                        limit + 1,
                    ),
                ).fetchall()
                tombstone_rows = connection.execute(
                    """
                    SELECT memory_id, backend_ref, deleted_revision, deleted_at, reason_code
                    FROM memory_tombstones
                    WHERE tenant_id = %s AND user_id = %s AND deleted_revision > %s
                    ORDER BY deleted_revision ASC
                    LIMIT %s
                    """,
                    (principal.tenant_id, principal.user_id, start_revision, limit + 1),
                ).fetchall()

        changes: list[tuple[int, str, Any]] = [
            (int(row[6]), "memory", row) for row in event_rows
        ] + [(int(row[2]), "tombstone", row) for row in tombstone_rows]
        changes.sort(key=lambda item: (item[0], item[1]))
        has_more = len(changes) > limit
        selected = changes[:limit]
        memories: list[dict[str, Any]] = []
        tombstones: list[dict[str, Any]] = []
        for revision, kind, row in selected:
            if kind == "tombstone":
                tombstones.append(
                    {
                        "memory_id": str(row[0]),
                        "backend_ref": str(row[1]),
                        "deleted_revision": revision,
                        "deleted_at": row[3].isoformat(),
                        "reason_code": str(row[4]),
                    }
                )
                continue
            encrypted = EncryptedPayload(bytes(row[7]), bytes(row[8]), str(row[9]))
            aad = (
                f"{principal.tenant_id}:{principal.user_id}:{row[0]}:{row[2]}:{row[1]}"
            ).encode("utf-8")
            try:
                envelope = self._cipher.decrypt_json(encrypted, aad=aad)
            except EncryptionError as exc:
                raise SyncProtocolError("SYNC_PAYLOAD_UNAVAILABLE") from exc
            event_payload = envelope.get("payload")
            if not isinstance(event_payload, dict):
                raise SyncProtocolError("SYNC_PAYLOAD_UNAVAILABLE")
            memories.append(
                {
                    "memory_id": str(row[5]),
                    "backend_ref": str(row[5]),
                    "server_revision": revision,
                    "source_event_id": str(row[1]),
                    "workspace_id": str(row[3]),
                    "scope": str(row[4]),
                    "status": "confirmed",
                    "content_role": "reference_data",
                    "instruction_like": False,
                    "content": str(event_payload.get("content") or ""),
                    "kind": str(event_payload.get("kind") or "note"),
                    "confidence": float(event_payload.get("confidence", 0.72)),
                }
            )
        next_revision = selected[-1][0] if selected else start_revision
        return {
            "sync_epoch": current_epoch,
            "policy_version": SYNC_POLICY_VERSION,
            "auth_epoch": {
                "device": principal.device_auth_epoch,
                "agent": principal.agent_auth_epoch,
            },
            "memories": memories,
            "tombstones": tombstones,
            "next_revision": next_revision,
            "has_more": has_more,
            "next_cursor": (
                self._encode_cursor(current_epoch, workspace_id, next_revision) if has_more else None
            ),
            "reset_required": False,
            "trace_id": f"tr_{uuid.uuid4().hex}",
        }

    def _read_sync_epoch(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_value FROM gateway_state WHERE state_key = 'sync_epoch'"
            ).fetchone()
        if row is None or not str(row[0]).startswith("sync_"):
            raise SyncProtocolError("SYNC_EPOCH_UNAVAILABLE")
        return str(row[0])

    def _advance_contiguous_sequence(self, device_id: str) -> list[int]:
        with self._connect() as connection:
            with connection.transaction():
                row = connection.execute(
                    "SELECT last_contiguous_event_seq FROM devices WHERE device_id = %s FOR UPDATE",
                    (device_id,),
                ).fetchone()
                if row is None:
                    raise AuthError("AUTH_INVALID", status=401)
                contiguous = int(row[0])
                sequences = [
                    int(item[0])
                    for item in connection.execute(
                        """
                        SELECT device_seq FROM gateway_events
                        WHERE device_id = %s AND device_seq > %s
                        ORDER BY device_seq ASC LIMIT 1001
                        """,
                        (device_id, contiguous),
                    )
                ]
                missing: list[int] = []
                for sequence in sequences:
                    expected = contiguous + 1
                    if sequence != expected:
                        missing = [expected]
                        break
                    contiguous = sequence
                connection.execute(
                    "UPDATE devices SET last_contiguous_event_seq = %s, updated_at = now() WHERE device_id = %s",
                    (contiguous, device_id),
                )
                return missing

    @staticmethod
    def _record_checkpoint(
        connection: Any,
        principal: Principal,
        workspace_id: str,
        revision: int,
        sync_epoch: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO sync_checkpoints (
              device_id, agent_installation_id, workspace_id, server_revision,
              sync_epoch, auth_epoch, device_auth_epoch, agent_auth_epoch, policy_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (device_id, agent_installation_id, workspace_id) DO UPDATE
            SET server_revision = EXCLUDED.server_revision,
                sync_epoch = EXCLUDED.sync_epoch,
                auth_epoch = EXCLUDED.auth_epoch,
                device_auth_epoch = EXCLUDED.device_auth_epoch,
                agent_auth_epoch = EXCLUDED.agent_auth_epoch,
                policy_version = EXCLUDED.policy_version,
                updated_at = now()
            """,
            (
                principal.device_id,
                principal.agent_installation_id,
                workspace_id,
                revision,
                sync_epoch,
                max(principal.device_auth_epoch, principal.agent_auth_epoch),
                principal.device_auth_epoch,
                principal.agent_auth_epoch,
                SYNC_POLICY_VERSION,
            ),
        )

    @staticmethod
    def _encode_cursor(sync_epoch: str, workspace_id: str, revision: int) -> str:
        raw = json.dumps(
            {"v": 1, "epoch": sync_epoch, "workspace": workspace_id, "revision": revision},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, sync_epoch: str, workspace_id: str) -> int:
        try:
            raw = base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode("ascii"))
            value = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(value, dict)
                or value.get("v") != 1
                or value.get("epoch") != sync_epoch
                or value.get("workspace") != workspace_id
            ):
                raise ValueError
            return _integer(value.get("revision"), "CURSOR_INVALID")
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, SyncProtocolError):
                raise
            raise SyncProtocolError("CURSOR_INVALID") from exc

    @staticmethod
    def _epoch_reset_response(current_epoch: str) -> dict[str, Any]:
        return {
            "sync_epoch": current_epoch,
            "policy_version": SYNC_POLICY_VERSION,
            "reset_required": True,
            "memories": [],
            "tombstones": [],
            "next_revision": 0,
            "has_more": False,
            "next_cursor": None,
            "trace_id": f"tr_{uuid.uuid4().hex}",
        }
