"""用于 Gateway 与 Sidecar 本地密文的最小 AES-GCM 封装。"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any


class EncryptionError(ValueError):
    """密钥格式或密文完整性校验失败。"""


@dataclass(frozen=True)
class EncryptedPayload:
    """可以直接存储到 BYTEA / SQLite BLOB 字段的密文包。"""

    ciphertext: bytes
    nonce: bytes
    key_version: str


class EventCipher:
    """将事件正文与其稳定身份绑定，避免密文被跨事件复用。"""

    def __init__(self, key: bytes, key_version: str = "v1") -> None:
        if len(key) != 32:
            raise EncryptionError("事件加密密钥必须为 32 字节")
        if not key_version or len(key_version) > 64:
            raise EncryptionError("事件加密密钥版本无效")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ModuleNotFoundError as exc:
            raise EncryptionError("缺少 cryptography 依赖") from exc
        self._aesgcm = AESGCM(key)
        self.key_version = key_version

    @classmethod
    def from_base64(cls, encoded_key: str, key_version: str = "v1") -> "EventCipher":
        try:
            padding = "=" * (-len(encoded_key) % 4)
            key = base64.urlsafe_b64decode((encoded_key + padding).encode("ascii"))
        except (UnicodeEncodeError, ValueError) as exc:
            raise EncryptionError("事件加密密钥必须是 URL-safe Base64") from exc
        return cls(key, key_version)

    @classmethod
    def from_environment(cls) -> "EventCipher":
        encoded_key = os.environ.get("MEMORY_EVENT_ENCRYPTION_KEY", "")
        key_version = os.environ.get("MEMORY_EVENT_KEY_VERSION", "v1")
        if not encoded_key:
            raise EncryptionError("缺少 MEMORY_EVENT_ENCRYPTION_KEY")
        return cls.from_base64(encoded_key, key_version)

    @staticmethod
    def generate_base64_key() -> str:
        """供受保护部署脚本生成一次性密钥；调用方不得打印返回值。"""

        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")

    def encrypt_bytes(self, plaintext: bytes, *, aad: bytes) -> EncryptedPayload:
        nonce = secrets.token_bytes(12)
        return EncryptedPayload(
            ciphertext=self._aesgcm.encrypt(nonce, plaintext, aad),
            nonce=nonce,
            key_version=self.key_version,
        )

    def decrypt_bytes(self, encrypted: EncryptedPayload, *, aad: bytes) -> bytes:
        if encrypted.key_version != self.key_version:
            raise EncryptionError("事件密钥版本不匹配")
        try:
            return self._aesgcm.decrypt(encrypted.nonce, encrypted.ciphertext, aad)
        except Exception as exc:  # cryptography 不暴露可安全处理的细粒度错误
            raise EncryptionError("事件密文完整性校验失败") from exc

    def encrypt_json(self, payload: dict[str, Any], *, aad: bytes) -> EncryptedPayload:
        plaintext = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self.encrypt_bytes(plaintext, aad=aad)

    def decrypt_json(self, encrypted: EncryptedPayload, *, aad: bytes) -> dict[str, Any]:
        try:
            value = json.loads(self.decrypt_bytes(encrypted, aad=aad).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EncryptionError("事件密文不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise EncryptionError("事件密文不是 JSON 对象")
        return value
