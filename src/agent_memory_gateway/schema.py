"""Gateway 元数据库的显式迁移与兼容性检查。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2026-07-11.1"
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
        "review_candidates",
        "schema_migrations",
        "sync_checkpoints",
        "tenants",
        "users",
        "workspace_bindings",
        "workspaces",
    }
)


class MigrationError(RuntimeError):
    """迁移或 schema 校验失败。"""


def default_schema_path() -> Path:
    """返回随仓库发布的元数据库基线脚本。"""

    return Path(__file__).resolve().parents[2] / "schema" / "memory_gateway.sql"


def read_schema(schema_path: str | Path | None = None) -> str:
    """读取 schema，确保脚本没有被静默替换为空文件。"""

    path = Path(schema_path) if schema_path else default_schema_path()
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise MigrationError(f"schema 文件为空：{path}")
    return text


def schema_checksum(schema_path: str | Path | None = None) -> str:
    """生成脚本内容校验值，防止同版本脚本被静默修改。"""

    return hashlib.sha256(read_schema(schema_path).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MetadataSchemaReport:
    """元数据库的只读兼容性结果。"""

    database: str
    tables: frozenset[str]
    migration_checksum: str | None
    expected_checksum: str

    @property
    def missing_tables(self) -> list[str]:
        return sorted(REQUIRED_METADATA_TABLES - self.tables)

    @property
    def version_applied(self) -> bool:
        return self.migration_checksum is not None

    @property
    def checksum_matches(self) -> bool:
        return self.migration_checksum == self.expected_checksum

    @property
    def compatible(self) -> bool:
        return not self.missing_tables and self.version_applied and self.checksum_matches

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tables"] = sorted(self.tables)
        result["missing_tables"] = self.missing_tables
        result["version_applied"] = self.version_applied
        result["checksum_matches"] = self.checksum_matches
        result["compatible"] = self.compatible
        return result


def _psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise MigrationError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
    return psycopg


def inspect_metadata_schema(
    dsn: str, schema_path: str | Path | None = None
) -> MetadataSchemaReport:
    """只读检查元数据库，绝不创建表或写入迁移记录。"""

    psycopg = _psycopg()
    expected_checksum = schema_checksum(schema_path)
    with psycopg.connect(dsn, autocommit=True) as connection:
        database = connection.execute("SELECT current_database()").fetchone()[0]
        tables = frozenset(
            row[0]
            for row in connection.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = current_schema()
                """
            )
        )
        migration_checksum: str | None = None
        if "schema_migrations" in tables:
            row = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = %s",
                (SCHEMA_VERSION,),
            ).fetchone()
            migration_checksum = row[0] if row else None
    return MetadataSchemaReport(
        database=database,
        tables=tables,
        migration_checksum=migration_checksum,
        expected_checksum=expected_checksum,
    )


def apply_metadata_schema(dsn: str, schema_path: str | Path | None = None) -> MetadataSchemaReport:
    """显式执行一次 schema 迁移，并以 advisory lock 串行化操作。"""

    psycopg = _psycopg()
    sql = read_schema(schema_path)
    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            tables = frozenset(
                row[0]
                for row in connection.execute(
                    """
                    SELECT tablename
                    FROM pg_tables
                    WHERE schemaname = current_schema()
                    """
                )
            )
            if "schema_migrations" in tables:
                row = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = %s",
                    (SCHEMA_VERSION,),
                ).fetchone()
                if row is not None:
                    if row[0] != checksum:
                        raise MigrationError("同一 schema 版本的校验值不一致，拒绝覆盖")
                    return inspect_metadata_schema(dsn, schema_path)

            connection.execute(sql)
            connection.execute(
                """
                INSERT INTO schema_migrations (version, checksum)
                VALUES (%s, %s)
                ON CONFLICT (version) DO NOTHING
                """,
                (SCHEMA_VERSION, checksum),
            )
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    return inspect_metadata_schema(dsn, schema_path)
