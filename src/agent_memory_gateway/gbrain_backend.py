"""把 Gateway 已确认候选以最小字段映射写入现有 GBrain。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .security import SensitiveContentScanner
from .gbrain import GBrainSchemaReport, REQUIRED_COLUMNS, REQUIRED_EXTENSIONS, REQUIRED_TABLES
from .gbrain_migrate import REQUIRED_ADAPTER_COLUMNS, REQUIRED_POLICIES, expected_checksums


class GBrainBackendError(RuntimeError):
    """GBrain 适配器拒绝的输入或数据库错误。"""


class GBrainSecurityError(GBrainBackendError):
    """防御性拒绝可能绕过 Gateway 安全闸门的正文。"""


class GBrainRetryableError(GBrainBackendError):
    """临时数据库或网络故障；worker 可以退避重试。"""


class GBrainContractError(GBrainBackendError):
    """引用、幂等键或状态不满足后端契约；不可盲目重试。"""


class GBrainSchemaIncompatibleError(GBrainBackendError):
    """现场 schema 或权限与适配器版本不兼容。"""


FACT_KIND_BY_MEMORY_KIND = {
    "decision": "commitment",
    "preference": "preference",
    "commitment": "commitment",
    "belief": "belief",
    "event": "event",
    "fact": "fact",
    "note": "fact",
}
BACKEND_REF_PATTERN = re.compile(r"gbrain:fact:([1-9][0-9]*)$")
PAGE_REF_PATTERN = re.compile(r"gbrain:page:([1-9][0-9]*)$")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}$")


@dataclass(frozen=True)
class GBrainFact:
    backend_ref: str
    fact_id: int
    source_id: str
    content: str
    kind: str
    confidence: float


def source_id_for_tenant(tenant_id: str) -> str:
    value = str(tenant_id).strip()
    if not value or len(value) > 128:
        raise GBrainBackendError("租户 ID 无效")
    return f"memory-gateway:{value}"


def fact_kind_for_memory_kind(kind: str) -> str:
    return FACT_KIND_BY_MEMORY_KIND.get(str(kind).strip().lower(), "fact")


def fact_id_from_ref(reference: str) -> int | None:
    match = BACKEND_REF_PATTERN.fullmatch(str(reference))
    return int(match.group(1)) if match else None


def page_id_from_ref(reference: str) -> int | None:
    match = PAGE_REF_PATTERN.fullmatch(str(reference))
    return int(match.group(1)) if match else None


class GBrainBackend:
    """所有写入使用 GBrain 内的 binding 表实现可恢复的幂等性。"""

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise GBrainBackendError("缺少 GBrain 连接串")
        self._dsn = dsn
        self._connection_factory = connection_factory

    def _connect(self, *, autocommit: bool = False) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        psycopg, _ = self._psycopg()
        return psycopg.connect(self._dsn, autocommit=autocommit)

    @staticmethod
    def _psycopg() -> Any:
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ModuleNotFoundError as exc:
            raise GBrainBackendError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
        return psycopg, Jsonb

    def health(self) -> bool:
        with self._connect(autocommit=True) as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    def schema_version(self) -> str:
        """返回原生 schema 指纹与 adapter 版本；不兼容时 fail-closed。"""

        try:
            with self._connect(autocommit=True) as connection:
                database = str(connection.execute("SELECT current_database()").fetchone()[0])
                extensions = frozenset(
                    str(row[0]) for row in connection.execute("SELECT extname FROM pg_extension")
                )
                tables = frozenset(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                    )
                )
                columns = frozenset(
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        """
                        SELECT table_name, column_name FROM information_schema.columns
                        WHERE table_schema = current_schema()
                        """
                    )
                )
                policies = frozenset(
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        """
                        SELECT tablename, policyname FROM pg_policies
                        WHERE schemaname = current_schema()
                        """
                    )
                )
                report = GBrainSchemaReport(database, extensions, tables, columns)
                if (
                    not report.compatible
                    or "memory_gateway_operations" not in tables
                    or not REQUIRED_ADAPTER_COLUMNS.issubset(columns)
                    or not REQUIRED_POLICIES.issubset(policies)
                ):
                    raise GBrainSchemaIncompatibleError("GBRAIN_SCHEMA_INCOMPATIBLE")
                adapter_migrations = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        "SELECT version, checksum FROM memory_gateway_adapter_migrations"
                    )
                }
            expected = expected_checksums()
            if any(adapter_migrations.get(version) != value for version, value in expected.items()):
                raise GBrainSchemaIncompatibleError("GBRAIN_SCHEMA_INCOMPATIBLE")
            adapter_versions = sorted(adapter_migrations)
            return f"{report.schema_version}+adapter-{','.join(adapter_versions)}"
        except GBrainBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate_database_error(exc) from None

    def upsert_confirmed(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        content: str,
        kind: str,
        confidence: float,
        allow_instruction_like: bool = False,
    ) -> str:
        """原子创建事实与 binding；重复调用返回第一次的 backend_ref。"""

        self._validate_idempotency_key(idempotency_key)
        content = str(content).strip()
        if not content or len(content) > 20_000:
            raise GBrainContractError("CONTENT_INVALID")
        assessment = SensitiveContentScanner().assess((content,))
        if assessment.has_sensitive_content:
            raise GBrainSecurityError("事实正文包含敏感信息")
        if assessment.instruction_like and not allow_instruction_like:
            raise GBrainSecurityError("命令式正文必须先经过隔离审核")
        confidence = float(confidence)
        if not 0 <= confidence <= 1:
            raise GBrainContractError("CONFIDENCE_INVALID")
        source_id = source_id_for_tenant(tenant_id)
        fact_kind = fact_kind_for_memory_kind(kind)
        try:
            _, Jsonb = self._psycopg()
            with self._connect() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (idempotency_key,),
                    )
                    operation = connection.execute(
                        "SELECT 1 FROM memory_gateway_operations WHERE idempotency_key = %s",
                        (idempotency_key,),
                    ).fetchone()
                    if operation is not None:
                        raise GBrainContractError("IDEMPOTENCY_KEY_REUSE")
                    existing = connection.execute(
                        "SELECT backend_ref FROM memory_gateway_bindings WHERE idempotency_key = %s",
                        (idempotency_key,),
                    ).fetchone()
                    if existing is not None:
                        return str(existing[0])
                    connection.execute(
                        """
                        INSERT INTO sources (id, name, config)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            source_id,
                            f"Memory Gateway ({tenant_id})",
                            Jsonb({"managed_by": "memory_gateway"}),
                        ),
                    )
                    fact_id = connection.execute(
                        """
                        INSERT INTO facts (
                          source_id, fact, kind, visibility, notability, source,
                          source_session, confidence
                        ) VALUES (%s, %s, %s, 'private', 'medium', 'memory_gateway', %s, %s)
                        RETURNING id
                        """,
                        (source_id, content, fact_kind, idempotency_key, confidence),
                    ).fetchone()[0]
                    backend_ref = f"gbrain:fact:{fact_id}"
                    connection.execute(
                        """
                        INSERT INTO memory_gateway_bindings (
                          idempotency_key, backend_ref, fact_id, source_id
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (idempotency_key, backend_ref, fact_id, source_id),
                    )
            return backend_ref
        except GBrainBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate_database_error(exc) from None

    def get_by_refs(self, references: Iterable[str]) -> list[GBrainFact]:
        ids = [fact_id for reference in references if (fact_id := fact_id_from_ref(reference)) is not None]
        if not ids:
            return []
        with self._connect(autocommit=True) as connection:
            rows = connection.execute(
                """
                SELECT id, source_id, fact, kind, confidence
                FROM facts
                WHERE id = ANY(%s)
                  AND expired_at IS NULL
                  AND superseded_by IS NULL
                """,
                (ids,),
            ).fetchall()
        return [
            GBrainFact(
                backend_ref=f"gbrain:fact:{row[0]}",
                fact_id=int(row[0]),
                source_id=str(row[1]),
                content=str(row[2]),
                kind=str(row[3]),
                confidence=float(row[4]),
            )
            for row in rows
        ]

    def search(self, *, allowed_references: Iterable[str], query: str, limit: int = 8) -> list[GBrainFact]:
        """只在 Gateway 预先授权的 fact ID 集合内全文匹配。"""

        ids = [fact_id for reference in allowed_references if (fact_id := fact_id_from_ref(reference)) is not None]
        if not ids:
            return []
        bounded_limit = max(1, min(int(limit), 50))
        normalized_query = str(query).strip()
        pattern = f"%{normalized_query}%"
        with self._connect(autocommit=True) as connection:
            rows = connection.execute(
                """
                SELECT id, source_id, fact, kind, confidence
                FROM facts
                WHERE id = ANY(%s)
                  AND expired_at IS NULL
                  AND superseded_by IS NULL
                  AND (%s = '' OR fact ILIKE %s)
                ORDER BY confidence DESC, created_at DESC
                LIMIT %s
                """,
                (ids, normalized_query, pattern, bounded_limit),
            ).fetchall()
        return [
            GBrainFact(
                backend_ref=f"gbrain:fact:{row[0]}",
                fact_id=int(row[0]),
                source_id=str(row[1]),
                content=str(row[2]),
                kind=str(row[3]),
                confidence=float(row[4]),
            )
            for row in rows
        ]

    def supersede(self, *, idempotency_key: str, old_ref: str, new_ref: str) -> str:
        self._validate_idempotency_key(idempotency_key)
        old_id = self._required_fact_id(old_ref)
        new_id = self._required_fact_id(new_ref)
        if old_id == new_id:
            raise GBrainContractError("GBRAIN_SUPERSEDE_SELF")
        try:
            with self._connect() as connection:
                with connection.transaction():
                    self._lock_operation(connection, idempotency_key)
                    existing = self._existing_operation(
                        connection, idempotency_key, "supersede", old_ref
                    )
                    if existing is not None:
                        return existing
                    source_id = self._require_managed_facts(connection, (old_id, new_id))
                    rows = connection.execute(
                        """
                        SELECT id, expired_at, superseded_by
                        FROM facts WHERE id = ANY(%s)
                        ORDER BY id FOR UPDATE
                        """,
                        ([old_id, new_id],),
                    ).fetchall()
                    states = {int(row[0]): row for row in rows}
                    if set(states) != {old_id, new_id}:
                        raise GBrainContractError("GBRAIN_REF_NOT_FOUND")
                    if any(row[1] is not None or row[2] is not None for row in states.values()):
                        raise GBrainContractError("GBRAIN_STATE_CONFLICT")
                    connection.execute(
                        """
                        UPDATE facts
                        SET superseded_by = %s, valid_until = COALESCE(valid_until, now())
                        WHERE id = %s
                        """,
                        (new_id, old_id),
                    )
                    self._insert_operation(
                        connection,
                        idempotency_key=idempotency_key,
                        operation="supersede",
                        source_id=source_id,
                        target_ref=old_ref,
                        result_ref=new_ref,
                    )
            return new_ref
        except GBrainBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate_database_error(exc) from None

    def archive(self, *, idempotency_key: str, reference: str) -> str:
        return self._set_expired_state(
            idempotency_key=idempotency_key,
            reference=reference,
            operation="archive",
            deleted_revision=None,
        )

    def restore_superseded(
        self,
        *,
        idempotency_key: str,
        old_ref: str,
        new_ref: str,
    ) -> str:
        """撤销一次明确取代：恢复旧事实，并将新事实归档保留历史。"""

        self._validate_idempotency_key(idempotency_key)
        old_id = self._required_fact_id(old_ref)
        new_id = self._required_fact_id(new_ref)
        if old_id == new_id:
            raise GBrainContractError("GBRAIN_SUPERSEDE_SELF")
        target_ref = f"{old_ref}->{new_ref}"
        try:
            with self._connect() as connection:
                with connection.transaction():
                    self._lock_operation(connection, idempotency_key)
                    existing = self._existing_operation(
                        connection, idempotency_key, "restore_superseded", target_ref
                    )
                    if existing is not None:
                        return existing
                    source_id = self._require_managed_facts(connection, (old_id, new_id))
                    rows = connection.execute(
                        """
                        SELECT id, expired_at, superseded_by
                        FROM facts WHERE id = ANY(%s)
                        ORDER BY id FOR UPDATE
                        """,
                        ([old_id, new_id],),
                    ).fetchall()
                    states = {int(row[0]): row for row in rows}
                    if set(states) != {old_id, new_id}:
                        raise GBrainContractError("GBRAIN_REF_NOT_FOUND")
                    old_state = states[old_id]
                    if old_state[1] is not None or old_state[2] != new_id:
                        raise GBrainContractError("GBRAIN_STATE_CONFLICT")
                    connection.execute(
                        """
                        UPDATE facts
                        SET superseded_by = NULL, valid_until = NULL
                        WHERE id = %s
                        """,
                        (old_id,),
                    )
                    connection.execute(
                        """
                        UPDATE facts
                        SET expired_at = COALESCE(expired_at, now()),
                            valid_until = COALESCE(valid_until, now())
                        WHERE id = %s
                        """,
                        (new_id,),
                    )
                    self._insert_operation(
                        connection,
                        idempotency_key=idempotency_key,
                        operation="restore_superseded",
                        source_id=source_id,
                        target_ref=target_ref,
                        result_ref=old_ref,
                        details={"new_ref": new_ref},
                    )
            return old_ref
        except GBrainBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate_database_error(exc) from None

    def reactivate(self, *, idempotency_key: str, reference: str) -> str:
        self._validate_idempotency_key(idempotency_key)
        fact_id = self._required_fact_id(reference)
        try:
            with self._connect() as connection:
                with connection.transaction():
                    self._lock_operation(connection, idempotency_key)
                    existing = self._existing_operation(
                        connection, idempotency_key, "reactivate", reference
                    )
                    if existing is not None:
                        return existing
                    source_id = self._require_managed_facts(connection, (fact_id,))
                    row = connection.execute(
                        "SELECT expired_at, superseded_by FROM facts WHERE id = %s FOR UPDATE",
                        (fact_id,),
                    ).fetchone()
                    if row is None:
                        raise GBrainContractError("GBRAIN_REF_NOT_FOUND")
                    tombstoned = connection.execute(
                        """
                        SELECT 1 FROM memory_gateway_operations
                        WHERE operation = 'tombstone' AND target_ref = %s
                        LIMIT 1
                        """,
                        (reference,),
                    ).fetchone()
                    if row[0] is None or row[1] is not None or tombstoned is not None:
                        raise GBrainContractError("GBRAIN_STATE_CONFLICT")
                    connection.execute(
                        """
                        UPDATE facts SET expired_at = NULL, valid_until = NULL
                        WHERE id = %s
                        """,
                        (fact_id,),
                    )
                    self._insert_operation(
                        connection,
                        idempotency_key=idempotency_key,
                        operation="reactivate",
                        source_id=source_id,
                        target_ref=reference,
                        result_ref=reference,
                    )
            return reference
        except GBrainBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate_database_error(exc) from None

    def tombstone(
        self, *, idempotency_key: str, reference: str, deleted_revision: int
    ) -> str:
        if isinstance(deleted_revision, bool) or int(deleted_revision) <= 0:
            raise GBrainContractError("DELETED_REVISION_INVALID")
        return self._set_expired_state(
            idempotency_key=idempotency_key,
            reference=reference,
            operation="tombstone",
            deleted_revision=int(deleted_revision),
        )

    def rebuild_crystal(
        self,
        *,
        idempotency_key: str,
        tenant_id: str,
        source_refs: Iterable[str],
        scope_binding_hash: str,
    ) -> str:
        self._validate_idempotency_key(idempotency_key)
        if not HASH_PATTERN.fullmatch(str(scope_binding_hash)):
            raise GBrainContractError("SCOPE_BINDING_HASH_INVALID")
        unique_refs = tuple(dict.fromkeys(str(reference) for reference in source_refs))
        pairs = sorted(
            ((self._required_fact_id(reference), reference) for reference in unique_refs),
            key=lambda item: item[0],
        )
        ids = tuple(item[0] for item in pairs)
        refs = tuple(item[1] for item in pairs)
        if len(ids) < 2 or len(ids) > 100:
            raise GBrainContractError("CRYSTAL_SOURCE_COUNT_INVALID")
        source_id = source_id_for_tenant(tenant_id)
        slug_hash = hashlib.sha256(
            (scope_binding_hash + "|" + "|".join(sorted(refs))).encode("utf-8")
        ).hexdigest()
        slug = f"memory-crystal-{slug_hash[:24]}"
        try:
            _, Jsonb = self._psycopg()
            with self._connect() as connection:
                with connection.transaction():
                    self._lock_operation(connection, idempotency_key)
                    existing = self._existing_operation(
                        connection, idempotency_key, "rebuild_crystal", f"scope:{scope_binding_hash}"
                    )
                    if existing is not None:
                        return existing
                    managed_source_id = self._require_managed_facts(connection, ids)
                    if managed_source_id != source_id:
                        raise GBrainContractError("CRYSTAL_SCOPE_OR_STATE_INVALID")
                    rows = connection.execute(
                        """
                        SELECT id, source_id, fact FROM facts
                        WHERE id = ANY(%s) AND expired_at IS NULL AND superseded_by IS NULL
                        ORDER BY id ASC FOR UPDATE
                        """,
                        (list(ids),),
                    ).fetchall()
                    if len(rows) != len(ids) or any(str(row[1]) != source_id for row in rows):
                        raise GBrainContractError("CRYSTAL_SCOPE_OR_STATE_INVALID")
                    compiled_truth = "\n".join(f"- {str(row[2])}" for row in rows)
                    content_hash = hashlib.sha256(compiled_truth.encode("utf-8")).hexdigest()
                    frontmatter = {
                        "managed_by": "memory_gateway",
                        "rule_version": "crystal-v1",
                        "scope_binding_hash": scope_binding_hash,
                        "source_refs": list(refs),
                    }
                    page_id = connection.execute(
                        """
                        INSERT INTO pages (
                          source_id, slug, type, page_kind, title, compiled_truth,
                          timeline, frontmatter, content_hash
                        ) VALUES (%s, %s, 'memory_crystal', 'markdown', %s, %s, '', %s, %s)
                        ON CONFLICT (source_id, slug) DO UPDATE SET
                          compiled_truth = EXCLUDED.compiled_truth,
                          frontmatter = EXCLUDED.frontmatter,
                          content_hash = EXCLUDED.content_hash,
                          deleted_at = NULL,
                          updated_at = now()
                        RETURNING id
                        """,
                        (
                            source_id,
                            slug,
                            "Shared memory crystal",
                            compiled_truth,
                            Jsonb(frontmatter),
                            content_hash,
                        ),
                    ).fetchone()[0]
                    page_ref = f"gbrain:page:{page_id}"
                    connection.execute(
                        "UPDATE facts SET consolidated_at = now() WHERE id = ANY(%s)",
                        (list(ids),),
                    )
                    self._insert_operation(
                        connection,
                        idempotency_key=idempotency_key,
                        operation="rebuild_crystal",
                        source_id=source_id,
                        target_ref=f"scope:{scope_binding_hash}",
                        result_ref=page_ref,
                        details={"source_count": len(refs), "source_refs_hash": slug_hash},
                    )
            return page_ref
        except GBrainBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate_database_error(exc) from None

    def _set_expired_state(
        self,
        *,
        idempotency_key: str,
        reference: str,
        operation: str,
        deleted_revision: int | None,
    ) -> str:
        self._validate_idempotency_key(idempotency_key)
        fact_id = self._required_fact_id(reference)
        try:
            with self._connect() as connection:
                with connection.transaction():
                    self._lock_operation(connection, idempotency_key)
                    existing = self._existing_operation(
                        connection, idempotency_key, operation, reference
                    )
                    if existing is not None:
                        return existing
                    source_id = self._require_managed_facts(connection, (fact_id,))
                    if operation == "archive":
                        tombstoned = connection.execute(
                            """
                            SELECT 1 FROM memory_gateway_operations
                            WHERE operation = 'tombstone' AND target_ref = %s
                            LIMIT 1
                            """,
                            (reference,),
                        ).fetchone()
                        if tombstoned is not None:
                            raise GBrainContractError("GBRAIN_STATE_CONFLICT")
                    connection.execute(
                        """
                        UPDATE facts
                        SET expired_at = COALESCE(expired_at, now()),
                            valid_until = COALESCE(valid_until, now())
                        WHERE id = %s
                        """,
                        (fact_id,),
                    )
                    self._insert_operation(
                        connection,
                        idempotency_key=idempotency_key,
                        operation=operation,
                        source_id=source_id,
                        target_ref=reference,
                        result_ref=reference,
                        deleted_revision=deleted_revision,
                    )
            return reference
        except GBrainBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._translate_database_error(exc) from None

    @staticmethod
    def _validate_idempotency_key(idempotency_key: str) -> None:
        if not idempotency_key or len(idempotency_key) > 256:
            raise GBrainContractError("IDEMPOTENCY_KEY_INVALID")

    @staticmethod
    def _required_fact_id(reference: str) -> int:
        fact_id = fact_id_from_ref(reference)
        if fact_id is None:
            raise GBrainContractError("GBRAIN_REF_INVALID")
        return fact_id

    @staticmethod
    def _lock_operation(connection: Any, idempotency_key: str) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (idempotency_key,),
        )

    @staticmethod
    def _existing_operation(
        connection: Any,
        idempotency_key: str,
        operation: str,
        target_ref: str,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT operation, target_ref, result_ref
            FROM memory_gateway_operations WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            binding = connection.execute(
                "SELECT 1 FROM memory_gateway_bindings WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if binding is not None:
                raise GBrainContractError("IDEMPOTENCY_KEY_REUSE")
            return None
        if str(row[0]) != operation or str(row[1]) != target_ref:
            raise GBrainContractError("IDEMPOTENCY_KEY_REUSE")
        return str(row[2]) if row[2] is not None else target_ref

    @staticmethod
    def _require_managed_facts(connection: Any, fact_ids: Iterable[int]) -> str:
        ids = tuple(dict.fromkeys(int(value) for value in fact_ids))
        rows = connection.execute(
            """
            SELECT binding.fact_id, binding.source_id
            FROM memory_gateway_bindings AS binding
            JOIN facts AS fact ON fact.id = binding.fact_id
            WHERE binding.fact_id = ANY(%s)
              AND binding.source_id = fact.source_id
            """,
            (list(ids),),
        ).fetchall()
        if {int(row[0]) for row in rows} != set(ids):
            raise GBrainContractError("GBRAIN_REF_NOT_MANAGED")
        source_ids = {str(row[1]) for row in rows}
        if len(source_ids) != 1:
            raise GBrainContractError("GBRAIN_SCOPE_MISMATCH")
        return next(iter(source_ids))

    def _insert_operation(
        self,
        connection: Any,
        *,
        idempotency_key: str,
        operation: str,
        source_id: str,
        target_ref: str,
        result_ref: str | None,
        deleted_revision: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        _, Jsonb = self._psycopg()
        connection.execute(
            """
            INSERT INTO memory_gateway_operations (
              idempotency_key, operation, source_id, target_ref, result_ref,
              deleted_revision, details_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                idempotency_key,
                operation,
                source_id,
                target_ref,
                result_ref,
                deleted_revision,
                Jsonb(details or {}),
            ),
        )

    def _translate_database_error(self, exc: BaseException) -> GBrainBackendError:
        psycopg, _ = self._psycopg()
        if isinstance(
            exc,
            (
                psycopg.OperationalError,
                psycopg.errors.AdminShutdown,
                psycopg.errors.CannotConnectNow,
                psycopg.errors.DeadlockDetected,
                psycopg.errors.LockNotAvailable,
                psycopg.errors.QueryCanceled,
                psycopg.errors.SerializationFailure,
            ),
        ):
            return GBrainRetryableError("GBRAIN_UNAVAILABLE")
        if isinstance(
            exc,
            (
                psycopg.errors.UndefinedTable,
                psycopg.errors.UndefinedColumn,
                psycopg.errors.InsufficientPrivilege,
            ),
        ):
            return GBrainSchemaIncompatibleError("GBRAIN_SCHEMA_INCOMPATIBLE")
        if isinstance(exc, psycopg.Error):
            return GBrainContractError("GBRAIN_OPERATION_FAILED")
        return GBrainBackendError("GBRAIN_INTERNAL_ERROR")
