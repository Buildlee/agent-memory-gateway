"""GBrain PostgreSQL 的只读 schema 检查。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


REQUIRED_EXTENSIONS = frozenset({"pg_trgm", "pgcrypto", "vector"})
REQUIRED_TABLES = frozenset(
    {
        "content_chunks",
        "facts",
        "links",
        "pages",
        "raw_data",
        "sources",
        "takes",
        "timeline_entries",
    }
)
REQUIRED_COLUMNS = frozenset(
    {
        ("facts", "id"),
        ("facts", "source_id"),
        ("facts", "fact"),
        ("facts", "kind"),
        ("facts", "expired_at"),
        ("facts", "superseded_by"),
        ("facts", "consolidated_at"),
        ("facts", "consolidated_into"),
        ("facts", "confidence"),
        ("pages", "id"),
        ("pages", "source_id"),
        ("pages", "slug"),
        ("pages", "compiled_truth"),
        ("pages", "frontmatter"),
        ("pages", "content_hash"),
        ("pages", "deleted_at"),
        ("sources", "id"),
        ("sources", "config"),
    }
)


@dataclass(frozen=True)
class GBrainSchemaReport:
    database: str
    extensions: frozenset[str]
    tables: frozenset[str]
    columns: frozenset[tuple[str, str]]

    @property
    def missing_extensions(self) -> list[str]:
        return sorted(REQUIRED_EXTENSIONS - self.extensions)

    @property
    def missing_tables(self) -> list[str]:
        return sorted(REQUIRED_TABLES - self.tables)

    @property
    def missing_columns(self) -> list[str]:
        return sorted(f"{table}.{column}" for table, column in REQUIRED_COLUMNS - self.columns)

    @property
    def compatible(self) -> bool:
        return not self.missing_extensions and not self.missing_tables and not self.missing_columns

    @property
    def schema_version(self) -> str:
        canonical = "|".join(
            sorted(self.extensions)
            + sorted(self.tables)
            + sorted(f"{table}.{column}" for table, column in self.columns)
        )
        return f"gbrain-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"

    def as_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "extensions": sorted(self.extensions),
            "tables": sorted(self.tables),
            "columns": sorted(f"{table}.{column}" for table, column in self.columns),
            "missing_extensions": self.missing_extensions,
            "missing_tables": self.missing_tables,
            "missing_columns": self.missing_columns,
            "schema_version": self.schema_version,
            "compatible": self.compatible,
        }


def inspect_schema(dsn: str) -> GBrainSchemaReport:
    """连接 GBrain 并只读取 schema 元数据。"""

    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc

    with psycopg.connect(dsn, autocommit=True) as connection:
        database = connection.execute("SELECT current_database()").fetchone()[0]
        extensions = frozenset(row[0] for row in connection.execute("SELECT extname FROM pg_extension"))
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
    return GBrainSchemaReport(database=database, extensions=extensions, tables=tables, columns=columns)
