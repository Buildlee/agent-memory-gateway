"""Gateway 部署前检查入口。"""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

from .gbrain import inspect_schema


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="只读检查 GBrain schema 是否兼容共享记忆 Gateway")
    parser.add_argument(
        "--gbrain-dsn",
        default=os.environ.get("MEMORY_GBRAIN_DSN"),
        help="从受保护环境变量或 secret 文件传入；不会在输出中显示",
    )
    args = parser.parse_args(argv)
    if not args.gbrain_dsn:
        parser.error("需要 --gbrain-dsn 或 MEMORY_GBRAIN_DSN")

    report = inspect_schema(args.gbrain_dsn)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    if not report.compatible:
        raise SystemExit(2)
