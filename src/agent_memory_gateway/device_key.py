"""生成不覆盖既有文件的 Ed25519 设备密钥。"""

from __future__ import annotations

import argparse
import base64
import os
import stat
from pathlib import Path
from typing import Sequence


def generate_device_key(output_path: str | Path) -> str:
    """写入 PEM 私钥并返回 URL-safe Base64 公钥；绝不打印私钥。"""

    path = Path(output_path)
    if path.exists():
        raise FileExistsError(f"拒绝覆盖已有设备私钥：{path}")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 cryptography 依赖") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(private_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")


def validate_device_key_file(path_value: str | Path) -> Path:
    """确认 Linux/NAS 上的设备私钥不是可被其他账号读取的文件。"""

    path = Path(path_value)
    if not path.is_file() or path.is_symlink():
        raise ValueError("DEVICE_KEY_INVALID")
    if os.name != "nt":
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise ValueError("DEVICE_KEY_INVALID") from exc
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("DEVICE_KEY_PERMISSIONS_INVALID")
    return path


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成 Ed25519 设备私钥，并输出可登记的公钥")
    parser.add_argument("--output", type=Path, required=True, help="私钥 PEM 输出路径；已有文件会拒绝覆盖")
    args = parser.parse_args(argv)
    public_key = generate_device_key(args.output)
    print(f"public_key={public_key}")
    print(f"private_key_path={args.output}")
