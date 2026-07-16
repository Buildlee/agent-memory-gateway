"""生成仅保存在本机的 Sidecar outbox key 文件。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from .crypto import EventCipher


def generate_sidecar_key_file(output_path: str | Path) -> Path:
    """写入本机环境文件；已有文件一律拒绝覆盖。"""

    path = Path(output_path)
    if path.exists():
        raise FileExistsError(f"拒绝覆盖已有 Sidecar key 文件：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = EventCipher.generate_base64_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
        stream.write(f"MEMORY_OUTBOX_KEY={key}\nMEMORY_OUTBOX_KEY_VERSION=v1\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成本机 Sidecar 加密 outbox key 文件")
    parser.add_argument("--output", type=Path, required=True, help="本机受保护路径；已有文件会拒绝覆盖")
    args = parser.parse_args(argv)
    path = generate_sidecar_key_file(args.output)
    print(f"sidecar_key_file={path}")


if __name__ == "__main__":
    main()
