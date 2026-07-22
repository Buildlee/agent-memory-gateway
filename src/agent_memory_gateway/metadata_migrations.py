"""Gateway 元数据库的版本化、显式增量迁移。"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


BASE_SCHEMA_VERSION = "2026-07-11.1"
MIGRATION_LOCK_KEY = 740_110_001
REQUIRED_METADATA_TABLES = frozenset(
    {
        "agent_installations",
        "audit_log",
        "backend_bindings",
        "dead_letters",
        "devices",
        "event_receipts",
        "gateway_events",
        "gateway_state",
        "memory_tombstones",
        "pairing_codes",
        "refresh_credentials",
        "memory_lifecycle",
        "memory_lifecycle_history",
        "memory_crystals",
        "review_operations",
        "review_candidates",
        "schema_migrations",
        "sync_checkpoints",
        "tenants",
        "users",
        "workspace_bindings",
        "workspaces",
    }
)
REQUIRED_METADATA_COLUMNS = frozenset(
    {
        ("gateway_events", "scope"),
        ("gateway_events", "instruction_like"),
        ("memory_tombstones", "revoked_revision"),
        ("devices", "last_contiguous_event_seq"),
        ("refresh_credentials", "replacement_ciphertext"),
        ("refresh_credentials", "replacement_nonce"),
        ("refresh_credentials", "replacement_key_version"),
        ("sync_checkpoints", "device_auth_epoch"),
        ("sync_checkpoints", "agent_auth_epoch"),
        ("sync_checkpoints", "policy_version"),
        ("memory_lifecycle", "status"),
        ("memory_lifecycle", "namespace_key"),
        ("memory_lifecycle", "scope_binding_hash"),
        ("memory_crystals", "status"),
        ("review_operations", "idempotency_key"),
        ("review_candidates", "last_operation_id"),
    }
)


class MigrationError(RuntimeError):
    """迁移或 schema 校验失败。"""


@dataclass(frozen=True)
class MigrationSpec:
    version: str
    path: Path


def repository_root() -> Path:
    configured_root = os.environ.get("MEMORY_GATEWAY_REPOSITORY_ROOT")
    if configured_root:
        return Path(configured_root).resolve()
    return Path(__file__).resolve().parents[2]


def schema_directory() -> Path:
    """返回运行时可读取的迁移文件目录。

    容器和源码开发优先使用仓库根目录的 schema；安装 wheel 后该目录不在
    site-packages 中，改用随包发布的只读副本。
    """

    checkout_schema = repository_root() / "schema"
    if checkout_schema.is_dir():
        return checkout_schema
    return Path(__file__).resolve().parent / "_schema"


def migration_specs(schema_path: str | Path | None = None) -> tuple[MigrationSpec, ...]:
    base_path = Path(schema_path) if schema_path else schema_directory() / "memory_gateway.sql"
    specs = [MigrationSpec(BASE_SCHEMA_VERSION, base_path)]
    if schema_path is None:
        specs.append(
            MigrationSpec(
                "2026-07-11.2",
                schema_directory() / "migrations" / "20260711_2_gateway_event_scope.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-11.3",
                schema_directory() / "migrations" / "20260711_3_refresh_replay.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-12.1",
                schema_directory() / "migrations" / "20260712_1_gateway_event_instruction_like.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-12.2",
                schema_directory() / "migrations" / "20260712_2_sync_protocol_state.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-13.4",
                schema_directory() / "migrations" / "20260713_4_review_lifecycle.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-13.5",
                schema_directory() / "migrations" / "20260713_5_crystal_state.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-22.1",
                schema_directory() / "migrations" / "20260722_1_tombstone_reactivation.sql",
            )
        )
    return tuple(specs)


def read_schema(path: str | Path) -> str:
    resolved = Path(path)
    text = resolved.read_text(encoding="utf-8")
    if not text.strip():
        raise MigrationError(f"schema 文件为空：{resolved}")
    return text


def expected_checksums(schema_path: str | Path | None = None) -> dict[str, str]:
    return {
        spec.version: hashlib.sha256(read_schema(spec.path).encode("utf-8")).hexdigest()
        for spec in migration_specs(schema_path)
    }


@dataclass(frozen=True)
class MetadataSchemaReport:
    database: str
    tables: frozenset[str]
    columns: frozenset[tuple[str, str]]
    migration_checksums: dict[str, str]
    expected_checksums: dict[str, str]

    @property
    def missing_tables(self) -> list[str]:
        return sorted(REQUIRED_METADATA_TABLES - self.tables)

    @property
    def missing_columns(self) -> list[str]:
        return sorted(f"{table}.{column}" for table, column in REQUIRED_METADATA_COLUMNS - self.columns)

    @property
    def missing_migrations(self) -> list[str]:
        return sorted(set(self.expected_checksums) - set(self.migration_checksums))

    @property
    def checksum_matches(self) -> bool:
        return all(
            self.migration_checksums.get(version) == checksum
            for version, checksum in self.expected_checksums.items()
        )

    @property
    def compatible(self) -> bool:
        return not self.missing_tables and not self.missing_columns and not self.missing_migrations and self.checksum_matches

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tables"] = sorted(self.tables)
        result["columns"] = sorted(f"{table}.{column}" for table, column in self.columns)
        result["missing_tables"] = self.missing_tables
        result["missing_columns"] = self.missing_columns
        result["missing_migrations"] = self.missing_migrations
        result["checksum_matches"] = self.checksum_matches
        result["compatible"] = self.compatible
        return result


def _psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise MigrationError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
    return psycopg


def _read_report(connection: Any, schema_path: str | Path | None = None) -> MetadataSchemaReport:
    database = connection.execute("SELECT current_database()").fetchone()[0]
    tables = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
    )
    columns = frozenset(
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
            """
        )
    )
    checksums: dict[str, str] = {}
    if "schema_migrations" in tables:
        checksums = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT version, checksum FROM schema_migrations")
        }
    return MetadataSchemaReport(database, tables, columns, checksums, expected_checksums(schema_path))


def inspect_metadata_schema(dsn: str, schema_path: str | Path | None = None) -> MetadataSchemaReport:
    """只读检查，不创建表或写入迁移记录。"""

    psycopg = _psycopg()
    with psycopg.connect(dsn, autocommit=True) as connection:
        return _read_report(connection, schema_path)


def apply_metadata_schema(dsn: str, schema_path: str | Path | None = None) -> MetadataSchemaReport:
    """按版本顺序执行尚未登记的脚本，拒绝同版本内容漂移。"""

    psycopg = _psycopg()
    expected = expected_checksums(schema_path)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            existing = _read_report(connection, schema_path).migration_checksums
            for spec in migration_specs(schema_path):
                checksum = expected[spec.version]
                applied_checksum = existing.get(spec.version)
                if applied_checksum is not None:
                    if applied_checksum != checksum:
                        raise MigrationError(f"迁移 {spec.version} 的校验值不一致，拒绝覆盖")
                    continue
                connection.execute(read_schema(spec.path))
                connection.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (spec.version, checksum),
                )
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    return inspect_metadata_schema(dsn, schema_path)
