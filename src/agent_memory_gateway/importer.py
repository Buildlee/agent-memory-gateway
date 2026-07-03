"""本地旧记忆导入扫描器。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

from .security import has_sensitive_content


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def infer_scope(path: Path, text: str) -> str:
    name = path.name.lower()
    joined = f"{path} {text}".lower()
    if name == "user.md":
        return "user"
    if name == "soul.md":
        return "agent"
    if "device" in joined or "端口" in text or "路径" in text:
        return "device"
    if name == "memory.md":
        return "workspace"
    return "workspace"


def split_markdown(text: str) -> Iterable[str]:
    """将 Markdown 按标题、列表和段落切块。"""

    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                yield "\n".join(current).strip()
                current = []
            continue
        if stripped.startswith("#") or re.match(r"^[-*+]\s+", stripped):
            if current:
                yield "\n".join(current).strip()
            current = [stripped]
        else:
            current.append(stripped)
    if current:
        yield "\n".join(current).strip()


def scan(source: Path, batch: str, output: Path) -> None:
    records = []
    for path in source.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        for chunk in split_markdown(text):
            if len(chunk) < 8:
                continue
            sensitive = has_sensitive_content(chunk)
            records.append(
                {
                    "import_batch_id": batch,
                    "source_path": str(path),
                    "original_content_hash": content_hash(chunk),
                    "content": chunk,
                    "scope": infer_scope(path, chunk),
                    "status": "blocked_sensitive" if sensitive else "imported_candidate",
                }
            )
    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"已扫描 {len(records)} 条候选记忆，预览文件：{output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="扫描本地旧记忆并生成导入预览")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_cmd = sub.add_parser("scan")
    scan_cmd.add_argument("--source", required=True)
    scan_cmd.add_argument("--batch", required=True)
    scan_cmd.add_argument("--output")
    args = parser.parse_args()

    if args.command == "scan":
        output = Path(args.output or f"import-preview-{args.batch}.jsonl")
        scan(Path(args.source), args.batch, output)


if __name__ == "__main__":
    main()
