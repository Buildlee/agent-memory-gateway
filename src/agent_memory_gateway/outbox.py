"""Sidecar 加密 outbox：单写者序号、密文事件与确认后清理。"""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .crypto import EncryptedPayload, EncryptionError, EventCipher


class OutboxError(RuntimeError):
    """outbox 不可安全使用。"""


class LegacyOutboxError(OutboxError):
    """发现未加密旧队列，要求显式人工迁移。"""


class OutboxInUseError(OutboxError):
    """另一个 Sidecar 进程已经持有该 outbox。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Outbox:
    """每台设备一个加密 SQLite 队列，不使用进程内队列代替磁盘。"""

    def __init__(self, path: str | Path, cipher: EventCipher) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = cipher
        self._thread_lock = threading.RLock()
        self._lock_handle: Any | None = None
        self._closed = False
        self._acquire_process_lock()
        self.conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        try:
            self._init_schema()
            self._require_no_legacy_events()
        except Exception:
            self.conn.close()
            self._release_process_lock()
            raise

    def close(self) -> None:
        with self._thread_lock:
            if self._closed:
                return
            self.conn.close()
            self._closed = True
            self._release_process_lock()

    def _acquire_process_lock(self) -> None:
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise OutboxInUseError("outbox 已由另一个 Sidecar 进程打开") from exc
        self._lock_handle = handle

    def _release_process_lock(self) -> None:
        handle = self._lock_handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lock_handle = None

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox_state (
              state_key TEXT PRIMARY KEY,
              state_value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbox_events_v2 (
              id TEXT PRIMARY KEY,
              device_seq INTEGER NOT NULL UNIQUE,
              state TEXT NOT NULL CHECK (state IN ('pending', 'in_flight', 'retryable_failed', 'dead_letter')),
              payload_ciphertext BLOB NOT NULL,
              payload_nonce BLOB NOT NULL,
              payload_key_version TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_events_v2_state
              ON outbox_events_v2 (state, device_seq);

            CREATE TABLE IF NOT EXISTS outbox_events_v3 (
              id TEXT PRIMARY KEY,
              device_seq INTEGER NOT NULL UNIQUE,
              state TEXT NOT NULL CHECK (
                state IN ('pending', 'in_flight', 'acked', 'retryable_failed', 'dead_letter')
              ),
              payload_ciphertext BLOB NOT NULL,
              payload_nonce BLOB NOT NULL,
              payload_key_version TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT,
              first_failed_at TEXT,
              last_failed_at TEXT,
              last_error_code TEXT,
              last_trace_id TEXT,
              ack_id TEXT,
              server_revision INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_events_v3_state
              ON outbox_events_v3 (state, next_attempt_at, device_seq);

            CREATE TABLE IF NOT EXISTS sidecar_cache_v2 (
              cache_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              server_revision INTEGER NOT NULL,
              payload_ciphertext BLOB NOT NULL,
              payload_nonce BLOB NOT NULL,
              payload_key_version TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (workspace_id, cache_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sidecar_cache_v2_workspace_revision
              ON sidecar_cache_v2 (workspace_id, server_revision DESC);
            """
        )
        migrated = self.conn.execute(
            "SELECT 1 FROM outbox_state WHERE state_key = 'outbox_v3_migrated'"
        ).fetchone()
        if migrated is None:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO outbox_events_v3 (
                  id, device_seq, state, payload_ciphertext, payload_nonce,
                  payload_key_version, created_at, updated_at
                )
                SELECT id, device_seq,
                       CASE WHEN state = 'in_flight' THEN 'retryable_failed' ELSE state END,
                       payload_ciphertext, payload_nonce, payload_key_version,
                       created_at, updated_at
                FROM outbox_events_v2
                """
            )
            self.conn.execute(
                """
                INSERT INTO outbox_state (state_key, state_value, updated_at)
                VALUES ('outbox_v3_migrated', '1', ?)
                """,
                (utc_now(),),
            )
        cache_migrated = self.conn.execute(
            "SELECT 1 FROM outbox_state WHERE state_key = 'cache_v1_migrated'"
        ).fetchone()
        if cache_migrated is None:
            v1_exists = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sidecar_cache_v1'"
            ).fetchone()
            if v1_exists:
                v1_rows = self.conn.execute(
                    "SELECT workspace_id, cache_id, server_revision,"
                    " payload_ciphertext, payload_nonce, payload_key_version, updated_at"
                    " FROM sidecar_cache_v1"
                ).fetchall()
                for row in v1_rows:
                    workspace_id, cache_id, revision, ct, nonce, kv, updated = (
                        str(row[0]), str(row[1]), int(row[2]),
                        bytes(row[3]), bytes(row[4]), str(row[5]), str(row[6]),
                    )
                    try:
                        old_aad = f"memory-sidecar-cache:{cache_id}".encode("utf-8")
                        decrypted = self._cipher.decrypt_bytes(
                            EncryptedPayload(ct, nonce, kv), aad=old_aad
                        )
                        encrypted = self._cipher.encrypt_bytes(
                            decrypted, aad=self._cache_aad(workspace_id, cache_id)
                        )
                        self.conn.execute(
                            """
                            INSERT OR IGNORE INTO sidecar_cache_v2 (
                              workspace_id, cache_id, server_revision,
                              payload_ciphertext, payload_nonce, payload_key_version, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (workspace_id, cache_id, revision,
                             encrypted.ciphertext, encrypted.nonce,
                             encrypted.key_version, updated),
                        )
                    except EncryptionError:
                        pass  # skip corrupt entries
            self.conn.execute(
                """
                INSERT INTO outbox_state (state_key, state_value, updated_at)
                VALUES ('cache_v1_migrated', '1', ?)
                """,
                (utc_now(),),
            )
        self.conn.execute(
            """
            UPDATE outbox_events_v3
            SET state = 'retryable_failed', next_attempt_at = NULL, updated_at = ?
            WHERE state = 'in_flight'
            """,
            (utc_now(),),
        )
        self.conn.commit()

    def _require_no_legacy_events(self) -> None:
        table = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'outbox_events'"
        ).fetchone()
        if table is None:
            return
        count = self.conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0]
        if count:
            raise LegacyOutboxError("检测到未加密旧 outbox；请先导出并人工审核，不能自动迁移")

    def prepare_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """为新事件分配稳定 ID、单调设备序号和发生时间。"""

        event = dict(payload)
        event_id = str(event.get("event_id") or f"evt_{uuid.uuid4().hex}")
        now = utc_now()
        with self._thread_lock:
            with self.conn:
                row = self.conn.execute(
                    "SELECT state_value FROM outbox_state WHERE state_key = 'device_seq'"
                ).fetchone()
                next_sequence = int(row[0]) + 1 if row else 1
                self.conn.execute(
                    """
                    INSERT INTO outbox_state (state_key, state_value, updated_at)
                    VALUES ('device_seq', ?, ?)
                    ON CONFLICT (state_key) DO UPDATE
                    SET state_value = excluded.state_value, updated_at = excluded.updated_at
                    """,
                    (str(next_sequence), now),
                )
        event["event_id"] = event_id
        event["device_seq"] = next_sequence
        event.setdefault("occurred_at", now)
        event.setdefault("event_type", "memory.proposed")
        event.setdefault("schema_version", 1)
        return event

    def enqueue(self, payload: dict[str, Any]) -> str:
        event_id = str(payload.get("event_id") or "")
        device_seq = payload.get("device_seq")
        if not event_id or isinstance(device_seq, bool):
            raise OutboxError("加密 outbox 要求 event_id 和 device_seq")
        try:
            device_seq = int(device_seq)
        except (TypeError, ValueError) as exc:
            raise OutboxError("device_seq 无效") from exc
        encrypted = self._cipher.encrypt_json(payload, aad=self._aad(event_id))
        now = utc_now()
        with self._thread_lock:
            with self.conn:
                changed = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO outbox_events_v3 (
                      id, device_seq, state, payload_ciphertext, payload_nonce,
                      payload_key_version, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        device_seq,
                        encrypted.ciphertext,
                        encrypted.nonce,
                        encrypted.key_version,
                        now,
                        now,
                    ),
                ).rowcount
                if changed != 1:
                    raise OutboxError("event_id 或 device_seq 已在 outbox 中使用")
        return event_id

    def list_events(self) -> list[dict[str, Any]]:
        with self._thread_lock:
            rows = self.conn.execute(
                """
                SELECT id, payload_ciphertext, payload_nonce, payload_key_version
                FROM outbox_events_v3
                WHERE state IN ('pending', 'retryable_failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY device_seq ASC
                """
                ,
                (utc_now(),),
            ).fetchall()
            events: list[dict[str, Any]] = []
            for row in rows:
                encrypted = EncryptedPayload(bytes(row[1]), bytes(row[2]), str(row[3]))
                try:
                    event = self._cipher.decrypt_json(encrypted, aad=self._aad(str(row[0])))
                except EncryptionError as exc:
                    raise OutboxError(f"无法解密 outbox 事件 {row[0]}") from exc
                events.append(event)
            return events

    def mark_in_flight(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        with self._thread_lock:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE outbox_events_v3
                    SET state = 'in_flight', updated_at = ?
                    WHERE id IN (SELECT value FROM json_each(?))
                      AND state IN ('pending', 'retryable_failed')
                    """,
                    (utc_now(), json.dumps(event_ids)),
                )

    def mark_retryable(
        self,
        event_id: str,
        *,
        error_code: str = "NETWORK_ERROR",
        trace_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        with self._thread_lock:
            row = self.conn.execute(
                "SELECT attempt_count, first_failed_at FROM outbox_events_v3 WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row[0]) + 1
            delay = (
                max(0.0, min(float(retry_after_seconds), 900.0))
                if retry_after_seconds is not None
                else min(2.0 * (2 ** min(attempts - 1, 9)), 900.0) + random.uniform(0, 1)
            )
            now = datetime.now(timezone.utc)
            now_text = now.isoformat().replace("+00:00", "Z")
            next_attempt = (now + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE outbox_events_v3
                    SET state = 'retryable_failed', attempt_count = ?, next_attempt_at = ?,
                        first_failed_at = COALESCE(first_failed_at, ?), last_failed_at = ?,
                        last_error_code = ?, last_trace_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        attempts,
                        next_attempt,
                        now_text,
                        now_text,
                        str(error_code)[:64],
                        str(trace_id)[:128] if trace_id else None,
                        now_text,
                        event_id,
                    ),
                )

    def mark_terminal(self, event_id: str, result: dict[str, Any]) -> None:
        status = str(result.get("status") or "")
        terminal = status in {"applied", "duplicate"} or (
            status == "rejected" and not bool(result.get("retryable"))
        )
        if not terminal:
            raise OutboxError("只有终态回执可以标记 acked")
        revision = result.get("server_revision")
        if isinstance(revision, bool):
            revision = None
        with self._thread_lock:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE outbox_events_v3
                    SET state = 'acked', ack_id = ?, server_revision = ?,
                        last_error_code = ?, last_trace_id = ?, next_attempt_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(result.get("ack_id") or "")[:128] or None,
                        int(revision) if revision is not None else None,
                        str(result.get("error") or "")[:64] or None,
                        str(result.get("trace_id") or "")[:128] or None,
                        utc_now(),
                        event_id,
                    ),
                )

    def mark_dead_letter(self, event_id: str, *, error_code: str, trace_id: str | None = None) -> None:
        with self._thread_lock:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE outbox_events_v3
                    SET state = 'dead_letter', last_error_code = ?, last_trace_id = ?,
                        next_attempt_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(error_code)[:64], str(trace_id)[:128] if trace_id else None, utc_now(), event_id),
                )

    def cleanup_acked(self) -> int:
        """只清理已获得固定终态回执的本地密文事件。"""

        with self._thread_lock:
            with self.conn:
                changed = self.conn.execute(
                    "DELETE FROM outbox_events_v3 WHERE state = 'acked'"
                ).rowcount
            return int(changed)

    def remove(self, event_id: str) -> None:
        """兼容旧调用方；只允许移除已经持久化终态回执的事件。"""

        with self._thread_lock:
            with self.conn:
                changed = self.conn.execute(
                    "DELETE FROM outbox_events_v3 WHERE id = ? AND state = 'acked'",
                    (event_id,),
                ).rowcount
                if changed != 1:
                    raise OutboxError("拒绝移除尚未 acked 的 outbox 事件")

    def count(self) -> int:
        with self._thread_lock:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count FROM outbox_events_v3
                WHERE state IN ('pending', 'in_flight', 'retryable_failed')
                """
            ).fetchone()
            return int(row["count"])

    def status_counts(self) -> dict[str, int]:
        with self._thread_lock:
            rows = self.conn.execute(
                "SELECT state, count(*) FROM outbox_events_v3 GROUP BY state"
            ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows}

    def sync_state(self, workspace_id: str) -> dict[str, Any]:
        prefix = f"sync:{workspace_id}:"
        with self._thread_lock:
            rows = self.conn.execute(
                "SELECT state_key, state_value FROM outbox_state WHERE state_key LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        values = {str(row[0])[len(prefix) :]: str(row[1]) for row in rows}
        return {
            "sync_epoch": values.get("epoch", ""),
            "last_seen_revision": int(values.get("revision", "0")),
            "policy_version": values.get("policy_version", ""),
            "device_auth_epoch": int(values.get("device_auth_epoch", "0")),
            "agent_auth_epoch": int(values.get("agent_auth_epoch", "0")),
            "cache_as_of": values.get("cache_as_of"),
            "cursor": values.get("cursor") or None,
        }

    def clear_cache(self, workspace_id: str, sync_epoch: str) -> None:
        """epoch 或权限版本变化时先失效整个工作区缓存。"""

        with self._thread_lock:
            with self.conn:
                self.conn.execute("DELETE FROM sidecar_cache_v2 WHERE workspace_id = ?", (workspace_id,))
                self._set_sync_values(
                    workspace_id,
                    {
                        "epoch": sync_epoch,
                        "revision": "0",
                        "policy_version": "",
                        "device_auth_epoch": "0",
                        "agent_auth_epoch": "0",
                        "cache_as_of": "",
                        "cursor": "",
                    },
                )

    def apply_pull_page(self, workspace_id: str, result: dict[str, Any]) -> None:
        sync_epoch = str(result.get("sync_epoch") or "")
        if not sync_epoch:
            raise OutboxError("pull 结果缺少 sync_epoch")
        if bool(result.get("reset_required")):
            self.clear_cache(workspace_id, sync_epoch)
            return
        memories = result.get("memories") or []
        tombstones = result.get("tombstones") or []
        if not isinstance(memories, list) or not isinstance(tombstones, list):
            raise OutboxError("pull 结果结构无效")
        auth_epoch = result.get("auth_epoch") or {}
        if not isinstance(auth_epoch, dict):
            raise OutboxError("pull auth_epoch 无效")
        state = self.sync_state(workspace_id)
        if state["sync_epoch"] and state["sync_epoch"] != sync_epoch:
            raise OutboxError("sync_epoch 变化前必须先清空缓存")
        now = utc_now()
        encrypted_memories: list[tuple[str, int, EncryptedPayload]] = []
        for memory in memories:
            if not isinstance(memory, dict):
                raise OutboxError("pull memory 无效")
            if memory.get("content_role") != "reference_data" or bool(memory.get("instruction_like")):
                raise OutboxError("pull memory 违反引用数据安全边界")
            if str(memory.get("status") or "") not in {
                "confirmed",
                "superseded",
                "archived",
                "pending_deletion",
            }:
                raise OutboxError("pull memory 状态无效")
            cache_id = str(memory.get("memory_id") or "")
            revision = int(memory.get("server_revision") or 0)
            if not cache_id or revision <= 0:
                raise OutboxError("pull memory 缺少稳定 ID 或 revision")
            encrypted_memories.append(
                (
                    cache_id,
                    revision,
                    self._cipher.encrypt_json(memory, aad=self._cache_aad(workspace_id, cache_id)),
                )
            )
        with self._thread_lock:
            with self.conn:
                for cache_id, revision, encrypted in encrypted_memories:
                    self.conn.execute(
                        """
                        INSERT INTO sidecar_cache_v2 (
                          cache_id, workspace_id, server_revision, payload_ciphertext,
                          payload_nonce, payload_key_version, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (workspace_id, cache_id) DO UPDATE SET
                          server_revision = excluded.server_revision,
                          payload_ciphertext = excluded.payload_ciphertext,
                          payload_nonce = excluded.payload_nonce,
                          payload_key_version = excluded.payload_key_version,
                          updated_at = excluded.updated_at
                        WHERE excluded.server_revision >= sidecar_cache_v2.server_revision
                        """,
                        (
                            cache_id,
                            workspace_id,
                            revision,
                            encrypted.ciphertext,
                            encrypted.nonce,
                            encrypted.key_version,
                            now,
                        ),
                    )
                for tombstone in tombstones:
                    if not isinstance(tombstone, dict):
                        raise OutboxError("pull tombstone 无效")
                    backend_ref = str(tombstone.get("backend_ref") or "")
                    if backend_ref:
                        self.conn.execute(
                            "DELETE FROM sidecar_cache_v2 WHERE cache_id = ? AND workspace_id = ?",
                            (backend_ref, workspace_id),
                        )
                self._set_sync_values(
                    workspace_id,
                    {
                        "epoch": sync_epoch,
                        "revision": str(int(result.get("next_revision") or 0)),
                        "policy_version": str(result.get("policy_version") or ""),
                        "device_auth_epoch": str(int(auth_epoch.get("device") or 0)),
                        "agent_auth_epoch": str(int(auth_epoch.get("agent") or 0)),
                        "cache_as_of": now,
                        "cursor": str(result.get("next_cursor") or ""),
                    },
                )

    def cache_search(self, workspace_id: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 50))
        normalized_query = str(query).strip().lower()
        with self._thread_lock:
            rows = self.conn.execute(
                """
                SELECT cache_id, payload_ciphertext, payload_nonce, payload_key_version
                FROM sidecar_cache_v2
                WHERE workspace_id = ?
                ORDER BY server_revision DESC
                """,
                (workspace_id,),
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                encrypted = EncryptedPayload(bytes(row[1]), bytes(row[2]), str(row[3]))
                try:
                    memory = self._cipher.decrypt_json(
                        encrypted, aad=self._cache_aad(workspace_id, str(row[0]))
                    )
                except EncryptionError as exc:
                    raise OutboxError("无法解密 Sidecar 授权缓存") from exc
                if normalized_query and normalized_query not in str(memory.get("content") or "").lower():
                    continue
                results.append(memory)
                if len(results) >= bounded_limit:
                    break
            return results

    def pending_memories(self, workspace_id: str, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 100))
        normalized_query = str(query).strip().lower()
        with self._thread_lock:
            rows = self.conn.execute(
                """
                SELECT id, payload_ciphertext, payload_nonce, payload_key_version
                FROM outbox_events_v3
                WHERE state IN ('pending', 'in_flight', 'retryable_failed')
                ORDER BY device_seq ASC
                """
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                encrypted = EncryptedPayload(bytes(row[1]), bytes(row[2]), str(row[3]))
                try:
                    event = self._cipher.decrypt_json(encrypted, aad=self._aad(str(row[0])))
                except EncryptionError as exc:
                    raise OutboxError("无法解密本机待同步事件") from exc
                if str(event.get("workspace_id") or "") != workspace_id:
                    continue
                content = str(event.get("content") or "")
                if normalized_query and normalized_query not in content.lower():
                    continue
                results.append(
                    {
                        "memory_id": f"local:{row[0]}",
                        "source_event_id": str(row[0]),
                        "content": content,
                        "kind": str(event.get("kind") or "note"),
                        "scope": str(event.get("scope") or "workspace"),
                        "status": "pending_local",
                        "content_role": "reference_data",
                        "instruction_like": bool(event.get("instruction_like")),
                        "instruction_rule_ids": list(event.get("instruction_rule_ids") or []),
                    }
                )
                if len(results) >= bounded_limit:
                    break
            return results

    def _set_sync_values(self, workspace_id: str, values: dict[str, str]) -> None:
        now = utc_now()
        for key, value in values.items():
            self.conn.execute(
                """
                INSERT INTO outbox_state (state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (state_key) DO UPDATE SET
                  state_value = excluded.state_value, updated_at = excluded.updated_at
                """,
                (f"sync:{workspace_id}:{key}", value, now),
            )

    @staticmethod
    def _cache_aad(workspace_id: str, cache_id: str) -> bytes:
        return f"memory-sidecar-cache:{workspace_id}:{cache_id}".encode("utf-8")

    @staticmethod
    def _aad(event_id: str) -> bytes:
        return f"memory-sidecar-outbox:{event_id}".encode("utf-8")
