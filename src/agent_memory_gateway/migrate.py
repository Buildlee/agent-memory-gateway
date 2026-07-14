"""`memory-gateway migrate` 命令入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from .metadata_migrations import MigrationError, apply_metadata_schema, inspect_metadata_schema


def _print_report(report: object) -> None:
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="检查或显式迁移 Memory Gateway 元数据库")
    parser.add_argument(
        "--metadata-dsn",
        default=os.environ.get("MEMORY_METADATA_DSN"),
        help="从受保护环境变量或 secret 文件传入；不会在输出中显示",
    )
    parser.add_argument(
        "--schema-file",
        type=Path,
        help="默认使用仓库内 schema/memory_gateway.sql",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="只读检查表是否齐全")
    action.add_argument("--verify", action="store_true", help="核对迁移版本和脚本校验值")
    action.add_argument("--apply", action="store_true", help="显式执行建表脚本")
    args = parser.parse_args(argv)
    if not args.metadata_dsn:
        parser.error("需要 --metadata-dsn 或 MEMORY_METADATA_DSN")

    try:
        if args.apply:
            report = apply_metadata_schema(args.metadata_dsn, args.schema_file)
        else:
            report = inspect_metadata_schema(args.metadata_dsn, args.schema_file)
    except MigrationError as exc:
        parser.error(str(exc))

    _print_report(report)
    if args.check and not report.compatible:
        raise SystemExit(2)
    if args.verify and not report.compatible:
        raise SystemExit(2)
    if args.apply and not report.compatible:
        raise SystemExit(2)
