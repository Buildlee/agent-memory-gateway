"""签发和校验短期 Gateway 访问令牌。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable


class AccessTokenError(ValueError):
    """不向客户端泄露内部校验细节的访问令牌错误。"""


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode((value + padding).encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise AccessTokenError("令牌编码无效") from exc


@dataclass(frozen=True)
class AccessTokenClaims:
    tenant_id: str
    user_id: str
    device_id: str
    agent_installation_id: str
    device_auth_epoch: int
    agent_auth_epoch: int
    issued_at: int
    expires_at: int
    token_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AccessTokenClaims":
        expected = {
            "tenant_id",
            "user_id",
            "device_id",
            "agent_installation_id",
            "device_auth_epoch",
            "agent_auth_epoch",
            "issued_at",
            "expires_at",
            "token_id",
        }
        if set(payload) != expected:
            raise AccessTokenError("令牌声明无效")
        try:
            claims = cls(
                tenant_id=str(payload["tenant_id"]),
                user_id=str(payload["user_id"]),
                device_id=str(payload["device_id"]),
                agent_installation_id=str(payload["agent_installation_id"]),
                device_auth_epoch=int(payload["device_auth_epoch"]),
                agent_auth_epoch=int(payload["agent_auth_epoch"]),
                issued_at=int(payload["issued_at"]),
                expires_at=int(payload["expires_at"]),
                token_id=str(payload["token_id"]),
            )
        except (TypeError, ValueError) as exc:
            raise AccessTokenError("令牌声明无效") from exc
        text_values = (
            claims.tenant_id,
            claims.user_id,
            claims.device_id,
            claims.agent_installation_id,
            claims.token_id,
        )
        if (
            any(not value or len(value) > 256 for value in text_values)
            or claims.device_auth_epoch < 1
            or claims.agent_auth_epoch < 1
            or claims.expires_at <= claims.issued_at
        ):
            raise AccessTokenError("令牌声明无效")
        return claims


class AccessTokenSigner:
    """使用独立 HMAC 密钥签发最多 15 分钟的紧凑访问令牌。"""

    def __init__(
        self,
        key: bytes,
        *,
        key_version: str = "v1",
        ttl_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if len(key) != 32:
            raise AccessTokenError("访问令牌签名密钥必须为 32 字节")
        if not key_version or len(key_version) > 64:
            raise AccessTokenError("访问令牌签名密钥版本无效")
        if ttl_seconds < 60 or ttl_seconds > 900:
            raise AccessTokenError("访问令牌有效期必须在 60 到 900 秒之间")
        self._key = key
        self.key_version = key_version
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    @classmethod
    def from_base64(
        cls,
        encoded_key: str,
        *,
        key_version: str = "v1",
        ttl_seconds: int = 900,
        clock: Callable[[], float] = time.time,
    ) -> "AccessTokenSigner":
        return cls(_decode(encoded_key), key_version=key_version, ttl_seconds=ttl_seconds, clock=clock)

    @classmethod
    def from_environment(cls) -> "AccessTokenSigner":
        encoded_key = os.environ.get("MEMORY_ACCESS_TOKEN_SIGNING_KEY", "")
        if not encoded_key:
            raise AccessTokenError("缺少 MEMORY_ACCESS_TOKEN_SIGNING_KEY")
        key_version = os.environ.get("MEMORY_ACCESS_TOKEN_KEY_VERSION", "v1")
        return cls.from_base64(encoded_key, key_version=key_version)

    @staticmethod
    def generate_base64_key() -> str:
        return _encode(secrets.token_bytes(32))

    def issue(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        agent_installation_id: str,
        device_auth_epoch: int,
        agent_auth_epoch: int,
    ) -> tuple[str, AccessTokenClaims]:
        issued_at = int(self._clock())
        claims = AccessTokenClaims(
            tenant_id=tenant_id,
            user_id=user_id,
            device_id=device_id,
            agent_installation_id=agent_installation_id,
            device_auth_epoch=device_auth_epoch,
            agent_auth_epoch=agent_auth_epoch,
            issued_at=issued_at,
            expires_at=issued_at + self.ttl_seconds,
            token_id=secrets.token_urlsafe(18),
        )
        header = {"alg": "HS256", "kid": self.key_version, "typ": "AMG"}
        header_segment = _encode(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        payload_segment = _encode(
            json.dumps(asdict(claims), sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signed = f"{header_segment}.{payload_segment}".encode("ascii")
        signature = hmac.new(self._key, signed, hashlib.sha256).digest()
        return f"{signed.decode('ascii')}.{_encode(signature)}", claims

    def verify(self, token: str) -> AccessTokenClaims:
        if not token or len(token) > 4096:
            raise AccessTokenError("访问令牌无效")
        parts = token.split(".")
        if len(parts) != 3:
            raise AccessTokenError("访问令牌无效")
        header_segment, payload_segment, signature_segment = parts
        try:
            signed = f"{header_segment}.{payload_segment}".encode("ascii")
        except UnicodeEncodeError as exc:
            raise AccessTokenError("访问令牌无效") from exc
        expected = hmac.new(self._key, signed, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _decode(signature_segment)):
            raise AccessTokenError("访问令牌无效")
        try:
            header = json.loads(_decode(header_segment).decode("utf-8"))
            payload = json.loads(_decode(payload_segment).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccessTokenError("访问令牌无效") from exc
        if header != {"alg": "HS256", "kid": self.key_version, "typ": "AMG"} or not isinstance(payload, dict):
            raise AccessTokenError("访问令牌无效")
        claims = AccessTokenClaims.from_payload(payload)
        now = int(self._clock())
        if claims.issued_at > now + 60 or claims.expires_at <= now:
            raise AccessTokenError("访问令牌已过期")
        if claims.expires_at - claims.issued_at > 900:
            raise AccessTokenError("访问令牌有效期无效")
        return claims
