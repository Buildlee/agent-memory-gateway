"""加密保存刷新轮换响应，允许客户端在短窗口内安全重试。"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass


class RefreshReplayError(ValueError):
    """刷新重放密钥或密文无效。"""


@dataclass(frozen=True)
class EncryptedRefreshCredential:
    ciphertext: bytes
    nonce: bytes
    key_version: str


class RefreshReplayCipher:
    def __init__(self, key: bytes, *, key_version: str = "v1") -> None:
        if len(key) != 32:
            raise RefreshReplayError("刷新重放密钥必须为 32 字节")
        if not key_version or len(key_version) > 64:
            raise RefreshReplayError("刷新重放密钥版本无效")
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ModuleNotFoundError as exc:
            raise RefreshReplayError("缺少 cryptography 依赖") from exc
        self._cipher = AESGCM(key)
        self.key_version = key_version

    @classmethod
    def from_base64(cls, encoded_key: str, *, key_version: str = "v1") -> "RefreshReplayCipher":
        try:
            padding = "=" * (-len(encoded_key) % 4)
            key = base64.b64decode(
                (encoded_key + padding).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise RefreshReplayError("刷新重放密钥必须是 URL-safe Base64") from exc
        return cls(key, key_version=key_version)

    @classmethod
    def from_environment(cls) -> "RefreshReplayCipher":
        encoded_key = os.environ.get("MEMORY_REFRESH_REPLAY_KEY", "")
        if not encoded_key:
            raise RefreshReplayError("缺少 MEMORY_REFRESH_REPLAY_KEY")
        return cls.from_base64(
            encoded_key,
            key_version=os.environ.get("MEMORY_REFRESH_REPLAY_KEY_VERSION", "v1"),
        )

    @staticmethod
    def generate_base64_key() -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")

    def encrypt(self, credential: str, *, credential_id: str) -> EncryptedRefreshCredential:
        nonce = secrets.token_bytes(12)
        return EncryptedRefreshCredential(
            ciphertext=self._cipher.encrypt(nonce, credential.encode("utf-8"), credential_id.encode("utf-8")),
            nonce=nonce,
            key_version=self.key_version,
        )

    def decrypt(self, encrypted: EncryptedRefreshCredential, *, credential_id: str) -> str:
        if encrypted.key_version != self.key_version:
            raise RefreshReplayError("刷新重放密钥版本不匹配")
        try:
            plaintext = self._cipher.decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                credential_id.encode("utf-8"),
            )
            return plaintext.decode("utf-8")
        except Exception as exc:
            raise RefreshReplayError("刷新重放密文完整性校验失败") from exc
