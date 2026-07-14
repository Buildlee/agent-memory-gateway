"""使用 Windows Credential Manager 保存设备刷新凭据。"""

from __future__ import annotations

import ctypes
import hmac
import os
from ctypes import wintypes


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class WindowsCredentialError(RuntimeError):
    """Windows Credential Manager 调用失败。"""


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    if os.name != "nt":
        raise WindowsCredentialError("Windows Credential Manager 仅能在 Windows 上使用")
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
    ]
    library.CredReadW.restype = wintypes.BOOL
    library.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def read_generic_credential(target: str) -> tuple[str, str] | None:
    if not target or len(target) > 256:
        raise WindowsCredentialError("Credential Manager target 无效")
    library = _advapi32()
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not library.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return None
        raise WindowsCredentialError(f"读取 Windows 凭据失败，错误码：{error}")
    try:
        value = pointer.contents
        blob = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize)
        return str(value.UserName or ""), blob.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise WindowsCredentialError("Windows 凭据内容编码无效") from exc
    finally:
        library.CredFree(pointer)


def _write_generic_credential(target: str, username: str, secret: str) -> None:
    if not target or len(target) > 256 or not username or len(username) > 256:
        raise WindowsCredentialError("Credential Manager target 或 username 无效")
    if not secret:
        raise WindowsCredentialError("拒绝保存空凭据")
    blob = secret.encode("utf-16-le")
    if len(blob) > 2560:
        raise WindowsCredentialError("Windows 凭据内容过长")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = CREDENTIALW(
        Flags=0,
        Type=CRED_TYPE_GENERIC,
        TargetName=target,
        Comment="Agent Memory Gateway device refresh credential",
        LastWritten=FILETIME(),
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=username,
    )
    library = _advapi32()
    if not library.CredWriteW(ctypes.byref(credential), 0):
        raise WindowsCredentialError(f"写入 Windows 凭据失败，错误码：{ctypes.get_last_error()}")


def write_generic_credential(target: str, username: str, secret: str) -> None:
    if read_generic_credential(target) is not None:
        raise FileExistsError(f"拒绝覆盖已有 Windows 凭据：{target}")
    _write_generic_credential(target, username, secret)


def replace_generic_credential(
    target: str,
    username: str,
    *,
    expected_secret: str,
    new_secret: str,
) -> None:
    """仅在当前值与预期旧值一致时原子覆盖，用于服务端确认后的轮换。"""

    saved = read_generic_credential(target)
    if saved is None:
        raise WindowsCredentialError("待轮换的 Windows 凭据不存在")
    saved_username, saved_secret = saved
    if saved_username != username or not hmac.compare_digest(saved_secret, expected_secret):
        raise WindowsCredentialError("Windows 凭据已被其他进程更新，拒绝覆盖")
    if not new_secret or hmac.compare_digest(expected_secret, new_secret):
        raise WindowsCredentialError("新旧 Windows 凭据不能相同")
    _write_generic_credential(target, username, new_secret)
