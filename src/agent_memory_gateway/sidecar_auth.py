"""Sidecar 的短期访问令牌获取与 Windows 刷新凭据轮换。"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from .gateway_tls import gateway_ssl_context
from .windows_credential import (
    WindowsCredentialError,
    read_generic_credential,
    replace_generic_credential,
)


class SidecarAuthError(RuntimeError):
    """本机 Sidecar 无法安全取得短期访问令牌。"""


@dataclass(frozen=True)
class _CachedToken:
    value: str
    refresh_after: float


class WindowsRefreshTokenProvider:
    """仅由独立 Sidecar 读取刷新凭据并换取短期 token。"""

    def __init__(self, gateway_url: str, credential_target: str) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._credential_target = credential_target.strip()
        if not self._gateway_url.startswith(("http://", "https://")):
            raise SidecarAuthError("GATEWAY_URL_INVALID")
        if not self._credential_target or len(self._credential_target) > 256:
            raise SidecarAuthError("REFRESH_CREDENTIAL_TARGET_REQUIRED")
        self._ssl_context = gateway_ssl_context(self._gateway_url)
        self._lock = threading.RLock()
        self._tokens: dict[str, _CachedToken] = {}

    @classmethod
    def from_environment(cls) -> "WindowsRefreshTokenProvider":
        return cls(
            os.environ.get("MEMORY_GATEWAY_URL", "http://127.0.0.1:8787"),
            os.environ.get("MEMORY_REFRESH_CREDENTIAL_TARGET", ""),
        )

    def access_token(self, agent_installation_id: str) -> str:
        agent_installation_id = str(agent_installation_id or "").strip()
        if not agent_installation_id or len(agent_installation_id) > 256:
            raise SidecarAuthError("AGENT_INSTALLATION_ID_REQUIRED")
        with self._lock:
            cached = self._tokens.get(agent_installation_id)
            if cached is not None and cached.refresh_after > time.monotonic():
                return cached.value

            try:
                saved = read_generic_credential(self._credential_target)
            except WindowsCredentialError as exc:
                raise SidecarAuthError("REFRESH_CREDENTIAL_UNAVAILABLE") from exc
            if saved is None:
                raise SidecarAuthError("REFRESH_CREDENTIAL_MISSING")
            username, previous_credential = saved
            response = self._refresh(previous_credential, agent_installation_id)
            token = str(response.get("access_token") or "")
            replacement = str(response.get("refresh_credential") or "")
            expires_in = response.get("expires_in")
            if not token or not replacement or response.get("token_type") != "Bearer":
                raise SidecarAuthError("REFRESH_RESPONSE_INVALID")
            try:
                lifetime = int(expires_in)
            except (TypeError, ValueError) as exc:
                raise SidecarAuthError("REFRESH_RESPONSE_INVALID") from exc
            if lifetime <= 0:
                raise SidecarAuthError("REFRESH_RESPONSE_INVALID")
            try:
                replace_generic_credential(
                    self._credential_target,
                    username,
                    expected_secret=previous_credential,
                    new_secret=replacement,
                )
            except (WindowsCredentialError, FileExistsError) as exc:
                raise SidecarAuthError("REFRESH_CREDENTIAL_ROTATION_FAILED") from exc
            self._tokens[agent_installation_id] = _CachedToken(
                value=token,
                refresh_after=time.monotonic() + max(1, lifetime - 60),
            )
            return token

    def _refresh(self, refresh_credential: str, agent_installation_id: str) -> dict[str, Any]:
        body = json.dumps(
            {
                "refresh_credential": refresh_credential,
                "agent_installation_id": agent_installation_id,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        req = request.Request(
            self._gateway_url + "/v1/auth/refresh",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=10, context=self._ssl_context) as raw_response:  # noqa: S310
                response = json.loads(raw_response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                response = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                response = {}
            code = str(response.get("error") or "REFRESH_FAILED")
            raise SidecarAuthError(f"REFRESH_{code}") from None
        except (error.URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError):
            raise SidecarAuthError("GATEWAY_UNAVAILABLE") from None
        if not isinstance(response, dict):
            raise SidecarAuthError("REFRESH_RESPONSE_INVALID")
        return response
