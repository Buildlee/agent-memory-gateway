#!/usr/bin/env python3
"""稳定的 memory-device 启动器：始终转交给 runtime.json 中的当前版本。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def runtime_config() -> Path:
    override = str(os.environ.get("MEMORY_DEVICE_RUNTIME_CONFIG") or "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "memory-gateway" / "runtime.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "memory-gateway" / "runtime.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "memory-gateway" / "runtime.json"


def main() -> int:
    path = runtime_config()
    if path.is_symlink():
        raise SystemExit(f"运行配置不能是符号链接：{path}")
    if not path.exists():
        return subprocess.call(
            [sys.executable, "-m", "agent_memory_gateway.device_runtime", *sys.argv[1:]]
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"无法读取当前共享记忆运行配置：{path}") from exc
    executable = value.get("python_executable") if isinstance(value, dict) else None
    if not isinstance(executable, str) or not executable.strip():
        raise SystemExit(f"共享记忆运行配置缺少 python_executable：{path}")
    current = Path(executable)
    if not current.is_file():
        raise SystemExit(f"找不到当前共享记忆运行环境：{current}")
    return subprocess.call([str(current), "-m", "agent_memory_gateway.device_runtime", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
