"""`memory-gateway gbrain-migrate`：显式检查或迁移 GBrain adapter binding。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


BASE_SCHEMA_VERSION = "2026-07-11.1"
SCHEMA_VERSION = "2026-07-14.1"
MIGRATION_LOCK_KEY = 740_110_002
BASE_REQUIRED_TABLES = frozenset({"memory_gateway_adapter_migrations", "memory_gateway_bindings"})
REQUIRED_TABLES = BASE_REQUIRED_TABLES | frozenset({"memory_gateway_operations"})
REQUIRED_ADAPTER_COLUMNS = frozenset(
    {
        ("memory_gateway_bindings", "source_id"),
        ("memory_gateway_operations", "source_id"),
    }
)
REQUIRED_POLICIES = frozenset(
    {
        ("memory_gateway_adapter_migrations", "memory_gateway_migrations_select"),
        ("sources", "memory_gateway_sources_select"),
        ("sources", "memory_gateway_sources_insert"),
        ("facts", "memory_gateway_facts_select"),
        ("facts", "memory_gateway_facts_insert"),
        ("facts", "memory_gateway_facts_update"),
        ("pages", "memory_gateway_pages_select"),
        ("pages", "memory_gateway_pages_insert"),
        ("pages", "memory_gateway_pages_update"),
        ("memory_gateway_bindings", "memory_gateway_bindings_select"),
        ("memory_gateway_bindings", "memory_gateway_bindings_insert"),
        ("memory_gateway_operations", "memory_gateway_operations_select"),
        ("memory_gateway_operations", "memory_gateway_operations_insert"),
        ("timeline_entries", "memory_gateway_timeline_entries_select"),
    }
)


class GBrainMigrationError(RuntimeError):
    """GBrain adapter schema 不兼容或迁移失败。"""


def repository_root() -> Path:
    configured_root = os.environ.get("MEMORY_GATEWAY_REPOSITORY_ROOT")
    if configured_root:
        return Path(configured_root).resolve()
    return Path(__file__).resolve().parents[2]


def schema_directory() -> Path:
    """返回容器、源码目录或已安装包均可读取的 GBrain 迁移目录。"""

    checkout_schema = repository_root() / "schema"
    if checkout_schema.is_dir():
        return checkout_schema
    return Path(__file__).resolve().parent / "_schema"


def default_schema_path() -> Path:
    return schema_directory() / "gbrain_adapter.sql"


@dataclass(frozen=True)
class MigrationSpec:
    version: str
    path: Path


def migration_specs(schema_path: str | Path | None = None) -> tuple[MigrationSpec, ...]:
    base = Path(schema_path) if schema_path else default_schema_path()
    specs = [MigrationSpec(BASE_SCHEMA_VERSION, base)]
    if schema_path is None:
        specs.append(
            MigrationSpec(
                "2026-07-12.2",
                default_schema_path().parent / "gbrain_migrations" / "20260712_2_lifecycle_operations.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-13.1",
                default_schema_path().parent / "gbrain_migrations" / "20260713_1_runtime_schema_check.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-13.2",
                default_schema_path().parent / "gbrain_migrations" / "20260713_2_runtime_rls.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-13.3",
                default_schema_path().parent / "gbrain_migrations" / "20260713_3_page_trigger_read.sql",
            )
        )
        specs.append(
            MigrationSpec(
                "2026-07-14.1",
                default_schema_path().parent / "gbrain_migrations" / "20260714_1_restore_superseded.sql",
            )
        )
    return tuple(specs)


def read_schema(schema_path: str | Path | None = None) -> str:
    path = Path(schema_path) if schema_path else default_schema_path()
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise GBrainMigrationError(f"schema 文件为空：{path}")
    return text


def checksum(schema_path: str | Path | None = None) -> str:
    return hashlib.sha256(read_schema(schema_path).encode("utf-8")).hexdigest()


def expected_checksums(schema_path: str | Path | None = None) -> dict[str, str]:
    return {
        spec.version: hashlib.sha256(read_schema(spec.path).encode("utf-8")).hexdigest()
        for spec in migration_specs(schema_path)
    }


@dataclass(frozen=True)
class GBrainAdapterReport:
    database: str
    tables: frozenset[str]
    columns: frozenset[tuple[str, str]]
    policies: frozenset[tuple[str, str]]
    migration_checksums: dict[str, str]
    expected_checksums: dict[str, str]

    @property
    def missing_tables(self) -> list[str]:
        return sorted(REQUIRED_TABLES - self.tables)

    @property
    def missing_migrations(self) -> list[str]:
        return sorted(set(self.expected_checksums) - set(self.migration_checksums))

    @property
    def missing_columns(self) -> list[str]:
        return sorted(
            f"{table}.{column}" for table, column in REQUIRED_ADAPTER_COLUMNS - self.columns
        )

    @property
    def missing_policies(self) -> list[str]:
        return sorted(
            f"{table}.{policy}" for table, policy in REQUIRED_POLICIES - self.policies
        )

    @property
    def checksum_matches(self) -> bool:
        return all(
            self.migration_checksums.get(version) == value
            for version, value in self.expected_checksums.items()
        )

    @property
    def compatible(self) -> bool:
        return (
            not self.missing_tables
            and not self.missing_columns
            and not self.missing_policies
            and not self.missing_migrations
            and self.checksum_matches
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["tables"] = sorted(self.tables)
        result["columns"] = sorted(f"{table}.{column}" for table, column in self.columns)
        result["policies"] = sorted(f"{table}.{policy}" for table, policy in self.policies)
        result["missing_tables"] = self.missing_tables
        result["missing_columns"] = self.missing_columns
        result["missing_policies"] = self.missing_policies
        result["missing_migrations"] = self.missing_migrations
        result["checksum_matches"] = self.checksum_matches
        result["compatible"] = self.compatible
        return result


def _psycopg() -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise GBrainMigrationError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
    return psycopg


def inspect(dsn: str, schema_path: str | Path | None = None) -> GBrainAdapterReport:
    psycopg = _psycopg()
    expected = expected_checksums(schema_path)
    with psycopg.connect(dsn, autocommit=True) as connection:
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
        applied: dict[str, str] = {}
        if "memory_gateway_adapter_migrations" in tables:
            applied = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT version, checksum FROM memory_gateway_adapter_migrations"
                )
            }
    return GBrainAdapterReport(database, tables, columns, policies, applied, expected)


def apply(dsn: str, schema_path: str | Path | None = None) -> GBrainAdapterReport:
    psycopg = _psycopg()
    expected = expected_checksums(schema_path)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            tables = frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                )
            )
            applied: dict[str, str] = {}
            if "memory_gateway_adapter_migrations" in tables:
                applied = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        "SELECT version, checksum FROM memory_gateway_adapter_migrations"
                    )
                }
            for spec in migration_specs(schema_path):
                expected_value = expected[spec.version]
                applied_value = applied.get(spec.version)
                if applied_value is not None:
                    if applied_value != expected_value:
                        raise GBrainMigrationError(
                            f"GBrain adapter 迁移 {spec.version} 校验值不一致，拒绝覆盖"
                        )
                    continue
                connection.execute(read_schema(spec.path))
                connection.execute(
                    """
                    INSERT INTO memory_gateway_adapter_migrations (version, checksum)
                    VALUES (%s, %s)
                    """,
                    (spec.version, expected_value),
                )
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    return inspect(dsn, schema_path)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="检查或显式迁移 GBrain adapter binding")
    parser.add_argument("--gbrain-dsn", default=os.environ.get("MEMORY_GBRAIN_MIGRATOR_DSN"))
    parser.add_argument("--schema-file", type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    if not args.gbrain_dsn:
        parser.error("需要 --gbrain-dsn 或 MEMORY_GBRAIN_MIGRATOR_DSN")
    try:
        report = apply(args.gbrain_dsn, args.schema_file) if args.apply else inspect(args.gbrain_dsn, args.schema_file)
    except GBrainMigrationError as exc:
        parser.error(str(exc))
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    if (
        (args.check and (report.missing_tables or report.missing_migrations or not report.checksum_matches))
        or (args.verify and not report.compatible)
        or (args.apply and not report.compatible)
    ):
        raise SystemExit(2)
