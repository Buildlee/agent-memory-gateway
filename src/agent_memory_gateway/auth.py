"""Gateway 身份、能力和工作区边界。"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .access_token import AccessTokenError, AccessTokenSigner


class AuthError(ValueError):
    """对外可返回稳定错误码的鉴权错误。"""

    def __init__(self, code: str, status: int = 403) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Principal:
    """由服务端凭据确认后的请求主体。"""

    tenant_id: str
    user_id: str
    device_id: str
    agent_installation_id: str
    workspace_ids: frozenset[str]
    capabilities: frozenset[str]
    device_auth_epoch: int = 1
    agent_auth_epoch: int = 1
    token_id: str | None = None

    def require_workspace(self, workspace_id: str) -> None:
        if not workspace_id:
            raise AuthError("WORKSPACE_REQUIRED", status=400)
        if workspace_id not in self.workspace_ids:
            raise AuthError("WORKSPACE_FORBIDDEN")

    def require_capability(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise AuthError("CAPABILITY_FORBIDDEN")


class TokenAuthenticator:
    """读取受保护配置文件中的 token hash，不保存明文 token。"""

    def __init__(self, principals_by_hash: dict[str, Principal]) -> None:
        if not principals_by_hash:
            raise ValueError("至少需要配置一个 principal")
        self._principals_by_hash = principals_by_hash

    @classmethod
    def from_file(cls, path: str | Path) -> "TokenAuthenticator":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = raw.get("principals") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise ValueError("principal 配置必须是数组或包含 principals 数组的对象")

        principals: dict[str, Principal] = {}
        required = {
            "token_sha256",
            "tenant_id",
            "user_id",
            "device_id",
            "agent_installation_id",
            "workspace_ids",
            "capabilities",
        }
        for entry in entries:
            if not isinstance(entry, dict) or not required.issubset(entry):
                raise ValueError("principal 配置字段不完整")
            token_hash = str(entry["token_sha256"]).lower()
            if len(token_hash) != 64 or any(char not in "0123456789abcdef" for char in token_hash):
                raise ValueError("token_sha256 必须是 SHA-256 十六进制值")
            if token_hash in principals:
                raise ValueError("token_sha256 重复")
            workspace_ids = frozenset(str(value) for value in entry["workspace_ids"] if str(value))
            capabilities = frozenset(str(value) for value in entry["capabilities"] if str(value))
            if not workspace_ids or not capabilities:
                raise ValueError("workspace_ids 和 capabilities 不能为空")
            principals[token_hash] = Principal(
                tenant_id=str(entry["tenant_id"]),
                user_id=str(entry["user_id"]),
                device_id=str(entry["device_id"]),
                agent_installation_id=str(entry["agent_installation_id"]),
                workspace_ids=workspace_ids,
                capabilities=capabilities,
            )
        return cls(principals)

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("AUTH_REQUIRED", status=401)
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise AuthError("AUTH_REQUIRED", status=401)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        principal = self._principals_by_hash.get(token_hash)
        if principal is None:
            # 未命中时仍执行一次定长比较，避免通过响应时间区分 token 是否命中。
            hmac.compare_digest(token_hash, "0" * 64)
            raise AuthError("TOKEN_INVALID", status=401)
        return principal

    @staticmethod
    def validate_payload_identity(principal: Principal, payload: dict[str, Any]) -> None:
        expected = {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "device_id": principal.device_id,
            "agent_id": principal.agent_installation_id,
            "agent_installation_id": principal.agent_installation_id,
        }
        for field, value in expected.items():
            supplied = payload.get(field)
            if supplied is not None and str(supplied) != value:
                raise AuthError("IDENTITY_MISMATCH")
        workspace_id = payload.get("workspace_id")
        if workspace_id is not None:
            principal.require_workspace(str(workspace_id))


class PostgresTokenAuthenticator(TokenAuthenticator):
    """校验短期签名令牌，并从元数据库读取当前授权边界。"""

    def __init__(
        self,
        dsn: str,
        signer: AccessTokenSigner,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise ValueError("缺少元数据库运行连接串")
        self._dsn = dsn
        self._signer = signer
        self._connection_factory = connection_factory

    @staticmethod
    def _psycopg() -> Any:
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise RuntimeError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
        return psycopg

    def authenticate(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("AUTH_REQUIRED", status=401)
        token = authorization.removeprefix("Bearer ").strip()
        try:
            claims = self._signer.verify(token)
        except AccessTokenError as exc:
            raise AuthError("AUTH_INVALID", status=401) from exc
        connection_context = (
            self._connection_factory()
            if self._connection_factory is not None
            else self._psycopg().connect(self._dsn, autocommit=True)
        )
        with connection_context as connection:
            row = connection.execute(
                """
                SELECT d.tenant_id, d.user_id, d.status, d.auth_epoch,
                       a.device_id, a.status, a.auth_epoch
                FROM devices AS d
                JOIN agent_installations AS a ON a.device_id = d.device_id
                WHERE d.device_id = %s AND a.agent_installation_id = %s
                """,
                (claims.device_id, claims.agent_installation_id),
            ).fetchone()
            if row is None:
                raise AuthError("AUTH_INVALID", status=401)
            if (
                row[0] != claims.tenant_id
                or row[1] != claims.user_id
                or row[2] != "active"
                or int(row[3]) != claims.device_auth_epoch
                or row[4] != claims.device_id
                or row[5] != "active"
                or int(row[6]) != claims.agent_auth_epoch
            ):
                raise AuthError("AUTH_REVOKED", status=401)
            bindings = list(
                connection.execute(
                    """
                    SELECT w.workspace_id, b.capabilities
                    FROM workspace_bindings AS b
                    JOIN workspaces AS w ON w.workspace_id = b.workspace_id
                    WHERE b.agent_installation_id = %s
                      AND b.status = 'active'
                      AND w.status = 'active'
                      AND w.tenant_id = %s
                      AND w.user_id = %s
                    """,
                    (claims.agent_installation_id, claims.tenant_id, claims.user_id),
                )
            )
        workspace_ids = frozenset(str(binding[0]) for binding in bindings)
        capabilities = frozenset(str(value) for binding in bindings for value in binding[1])
        return Principal(
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            device_id=claims.device_id,
            agent_installation_id=claims.agent_installation_id,
            workspace_ids=workspace_ids,
            capabilities=capabilities,
            device_auth_epoch=claims.device_auth_epoch,
            agent_auth_epoch=claims.agent_auth_epoch,
            token_id=claims.token_id,
        )
