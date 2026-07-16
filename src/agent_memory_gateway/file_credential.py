"""用于 Linux Sidecar 的受限文件刷新凭据存储。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


class FileCredentialError(RuntimeError):
    """刷新凭据文件不可安全使用。"""


def _path(value: str | Path) -> Path:
    path = Path(value)
    if not path.name or path.is_symlink():
        raise FileCredentialError("REFRESH_CREDENTIAL_FILE_INVALID")
    if not path.parent.is_dir():
        raise FileCredentialError("REFRESH_CREDENTIAL_DIRECTORY_MISSING")
    return path


def _validate_mode(path: Path) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise FileCredentialError("REFRESH_CREDENTIAL_UNAVAILABLE") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise FileCredentialError("REFRESH_CREDENTIAL_FILE_PERMISSIONS_INVALID")


def _payload(username: str, secret: str) -> bytes:
    username = str(username or "").strip()
    secret = str(secret or "").strip()
    if not username or len(username) > 256 or not secret or len(secret) > 512:
        raise FileCredentialError("REFRESH_CREDENTIAL_INVALID")
    return json.dumps(
        {"version": 1, "username": username, "refresh_credential": secret},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def read_file_credential(path: str | Path) -> tuple[str, str] | None:
    """读取受限凭据文件；文件不存在时返回 ``None``。"""

    credential_path = _path(path)
    if not credential_path.exists():
        return None
    _validate_mode(credential_path)
    try:
        value = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise FileCredentialError("REFRESH_CREDENTIAL_FILE_INVALID") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise FileCredentialError("REFRESH_CREDENTIAL_FILE_INVALID")
    body = _payload(value.get("username", ""), value.get("refresh_credential", ""))
    parsed = json.loads(body.decode("utf-8"))
    return str(parsed["username"]), str(parsed["refresh_credential"])


def _write(path: Path, body: bytes, *, replace: bool) -> None:
    if not replace and path.exists():
        raise FileExistsError(f"拒绝覆盖已有刷新凭据文件：{path}")
    descriptor = None
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=False,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if not replace:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                raise FileExistsError(f"拒绝覆盖已有刷新凭据文件：{path}") from None
            os.unlink(temporary_path)
            temporary_path = None
        else:
            os.replace(temporary_path, path)
            temporary_path = None
    except FileExistsError:
        raise
    except OSError as exc:
        raise FileCredentialError("REFRESH_CREDENTIAL_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def write_file_credential(path: str | Path, username: str, secret: str) -> None:
    """创建新的 0600 刷新凭据文件；已有文件一律拒绝覆盖。"""

    _write(_path(path), _payload(username, secret), replace=False)


def replace_file_credential(
    path: str | Path,
    username: str,
    *,
    expected_secret: str,
    new_secret: str,
) -> None:
    """在当前值匹配时原子轮换刷新凭据。"""

    credential_path = _path(path)
    existing = read_file_credential(credential_path)
    if existing is None or existing[0] != username or existing[1] != expected_secret:
        raise FileExistsError("REFRESH_CREDENTIAL_ROTATION_CONFLICT")
    _write(credential_path, _payload(username, new_secret), replace=True)
