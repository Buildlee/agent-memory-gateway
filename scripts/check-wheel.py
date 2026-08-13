from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath


REQUIRED_FILES = {
    "agent_memory_gateway/device_runtime.py",
    "agent_memory_gateway/device_lifecycle.py",
    "agent_memory_gateway/_schema/migrations/20260812_1_cross_platform_device_types.sql",
    "agent_memory_gateway/_schema/migrations/20260812_2_openclaw_agent_type.sql",
    "agent_memory_gateway/_schema/migrations/20260813_1_crystal_candidates.sql",
}
FORBIDDEN_NAME = re.compile(
    r"(?:^|/)(?:__pycache__|\.pytest_cache|\.mypy_cache)(?:/|$)|"
    r"(?:^|/).*(?:_tmp|\.tmp|\.bak)\.py$|\.py[co]$",
    re.IGNORECASE,
)
FORBIDDEN_TEXT = (
    "C:/Users/",
    "C:\\Users\\",
    "/Users/",
    "/home/",
    "AppData/Local/hermes/workspace",
    "AppData\\Local\\hermes\\workspace",
)


def _read_text(archive: zipfile.ZipFile, name: str) -> str:
    return archive.read(name).decode("utf-8", errors="replace")


def check_wheel(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file() or path.suffix != ".whl":
        return [f"wheel 不存在或扩展名无效：{path}"]

    with zipfile.ZipFile(path) as archive:
        names = [entry.filename for entry in archive.infolist()]
        name_set = set(names)

        missing = sorted(REQUIRED_FILES - name_set)
        errors.extend(f"wheel 缺少必要文件：{name}" for name in missing)

        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or "\\" in name:
                errors.append(f"wheel 包含不安全路径：{name}")
            if FORBIDDEN_NAME.search(name):
                errors.append(f"wheel 包含临时或缓存文件：{name}")

        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entry_points) != 1:
            errors.append("wheel 必须包含唯一 entry_points.txt")
        else:
            text = _read_text(archive, entry_points[0])
            if "memory-device = agent_memory_gateway.device_runtime:main" not in text:
                errors.append("wheel 缺少 memory-device 命令入口")

        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            errors.append("wheel 必须包含唯一 METADATA")
        else:
            text = _read_text(archive, metadata[0])
            if "Requires-Python: >=3.10" not in text:
                errors.append("wheel 的 Python 版本要求不是 >=3.10")
            if "License-Expression: MIT" not in text:
                errors.append("wheel 缺少 MIT SPDX 许可证元数据")

        for entry in archive.infolist():
            if entry.file_size > 2 * 1024 * 1024 or not entry.filename.endswith(
                (".py", ".json", ".toml", ".txt", ".md", ".sql")
            ):
                continue
            text = _read_text(archive, entry.filename)
            for marker in FORBIDDEN_TEXT:
                if marker in text:
                    errors.append(f"wheel 文件包含本机绝对路径：{entry.filename}")
                    break
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查发布 wheel 的文件与元数据")
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args(argv)
    errors = check_wheel(args.wheel)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"wheel_check=clean ({args.wheel.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
