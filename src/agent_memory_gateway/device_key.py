"""生成不覆盖既有文件的 Ed25519 设备密钥。"""

from __future__ import annotations

import argparse
import base64
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
    try:
        path.write_bytes(private_bytes)
    except Exception:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
        raise
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="生成 Ed25519 设备私钥，并输出可登记的公钥")
    parser.add_argument("--output", type=Path, required=True, help="私钥 PEM 输出路径；已有文件会拒绝覆盖")
    args = parser.parse_args(argv)
    public_key = generate_device_key(args.output)
    print(f"public_key={public_key}")
    print(f"private_key_path={args.output}")
