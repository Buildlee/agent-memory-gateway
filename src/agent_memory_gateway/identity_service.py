"""设备配对、刷新凭据轮换和撤销。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from .access_token import AccessTokenSigner
from .auth import AuthError
from .bootstrap import VALID_AGENT_TYPES, VALID_DEVICE_TYPES
from .metadata_store import MetadataStoreError
from .refresh_replay import EncryptedRefreshCredential, RefreshReplayCipher, RefreshReplayError


PAIRING_TTL_SECONDS = 600
REFRESH_TTL_DAYS = 90
REFRESH_REPLAY_SECONDS = 30


def _audit(
    connection: Any,
    *,
    tenant_id: str,
    actor_type: str,
    actor_id: str,
    action: str,
    result_code: str,
    device_id: str | None = None,
    agent_installation_id: str | None = None,
    workspace_id: str | None = None,
    target_ref: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_log (
          tenant_id, actor_type, actor_id, action, result_code, trace_id,
          device_id, agent_installation_id, workspace_id, target_ref, details_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            tenant_id,
            actor_type,
            actor_id,
            action,
            result_code,
            f"auth_{secrets.token_urlsafe(18)}",
            device_id,
            agent_installation_id,
            workspace_id,
            target_ref,
            json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
        ),
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decode_urlsafe(value: str, *, expected_length: int, field: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode((value + padding).encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise AuthError(f"{field.upper()}_INVALID", status=400) from exc
    if len(decoded) != expected_length:
        raise AuthError(f"{field.upper()}_INVALID", status=400)
    return decoded


def pairing_proof_message(pairing_code: str, device_id: str, nonce: str) -> bytes:
    """返回由待配对设备私钥签名的稳定消息。"""

    if not pairing_code or not device_id or not nonce:
        raise AuthError("PAIR_PROOF_INVALID", status=400)
    if any(len(value) > 512 for value in (pairing_code, device_id, nonce)):
        raise AuthError("PAIR_PROOF_INVALID", status=400)
    return f"memory-gateway-pair-v1\n{pairing_code}\n{device_id}\n{nonce}".encode("utf-8")


def verify_pairing_proof(
    *,
    pairing_code: str,
    device_id: str,
    nonce: str,
    public_key: str,
    proof_signature: str,
) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ModuleNotFoundError as exc:
        raise MetadataStoreError("缺少 cryptography 依赖") from exc
    public_bytes = _decode_urlsafe(public_key, expected_length=32, field="public_key")
    signature = _decode_urlsafe(proof_signature, expected_length=64, field="proof_signature")
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature,
            pairing_proof_message(pairing_code, device_id, nonce),
        )
    except InvalidSignature as exc:
        raise AuthError("PAIR_PROOF_INVALID", status=403) from exc


def _validate_identifier(name: str, value: Any) -> str:
    text = str(value).strip()
    if not text or len(text) > 256:
        raise AuthError(f"{name.upper()}_INVALID", status=400)
    return text


def new_refresh_credential() -> tuple[str, str, str]:
    credential_id = f"rfc_{secrets.token_urlsafe(12)}"
    credential = f"{credential_id}.{secrets.token_urlsafe(32)}"
    return credential_id, credential, _hash(credential)


def _parse_refresh_credential(value: Any) -> tuple[str, str]:
    credential = str(value or "").strip()
    if len(credential) > 512 or credential.count(".") != 1:
        raise AuthError("REFRESH_INVALID", status=401)
    credential_id, _ = credential.split(".", 1)
    if not credential_id.startswith("rfc_"):
        raise AuthError("REFRESH_INVALID", status=401)
    return credential_id, _hash(credential)


@dataclass(frozen=True)
class PairingAgent:
    agent_installation_id: str
    agent_type: str
    display_name: str

    @classmethod
    def from_payload(cls, payload: Any) -> "PairingAgent":
        if not isinstance(payload, dict):
            raise AuthError("PAIR_AGENTS_INVALID", status=400)
        agent_type = _validate_identifier("agent_type", payload.get("agent_type"))
        if agent_type not in VALID_AGENT_TYPES:
            raise AuthError("AGENT_TYPE_INVALID", status=400)
        return cls(
            agent_installation_id=_validate_identifier(
                "agent_installation_id", payload.get("agent_installation_id")
            ),
            agent_type=agent_type,
            display_name=_validate_identifier("agent_name", payload.get("display_name")),
        )


class IdentityAdmin:
    """只允许管理员 CLI 调用的配对码与撤销操作。"""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise MetadataStoreError("缺少元数据库迁移连接串")
        self._dsn = dsn

    @staticmethod
    def _psycopg() -> Any:
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise MetadataStoreError('缺少 PostgreSQL 依赖，请安装：pip install -e ".[postgres]"') from exc
        return psycopg

    def create_pairing_code(
        self,
        *,
        tenant_id: str,
        user_id: str,
        allowed_device_type: str,
        allowed_agent_types: tuple[str, ...],
        ttl_seconds: int = PAIRING_TTL_SECONDS,
    ) -> dict[str, Any]:
        tenant_id = _validate_identifier("tenant_id", tenant_id)
        user_id = _validate_identifier("user_id", user_id)
        if allowed_device_type not in VALID_DEVICE_TYPES:
            raise AuthError("DEVICE_TYPE_INVALID", status=400)
        agent_types = tuple(sorted(set(allowed_agent_types)))
        if not agent_types or any(value not in VALID_AGENT_TYPES for value in agent_types):
            raise AuthError("AGENT_TYPES_INVALID", status=400)
        if ttl_seconds < 60 or ttl_seconds > PAIRING_TTL_SECONDS:
            raise AuthError("PAIRING_TTL_INVALID", status=400)
        code_id = f"pair_{secrets.token_urlsafe(12)}"
        code = f"{code_id}.{secrets.token_urlsafe(18)}"
        psycopg = self._psycopg()
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                principal = connection.execute(
                    """
                    SELECT t.status, u.status
                    FROM tenants AS t
                    JOIN users AS u ON u.tenant_id = t.tenant_id
                    WHERE t.tenant_id = %s AND u.user_id = %s
                    """,
                    (tenant_id, user_id),
                ).fetchone()
                if principal is None or tuple(principal) != ("active", "active"):
                    raise AuthError("PAIRING_OWNER_INVALID", status=400)
                connection.execute(
                    """
                    INSERT INTO pairing_codes (
                      pairing_code_id, tenant_id, user_id, code_hash,
                      allowed_device_type, allowed_agent_types, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, now() + (%s * interval '1 second'))
                    """,
                    (code_id, tenant_id, user_id, _hash(code), allowed_device_type, list(agent_types), ttl_seconds),
                )
                _audit(
                    connection,
                    tenant_id=tenant_id,
                    actor_type="admin",
                    actor_id=user_id,
                    action="auth.pairing_code.create",
                    result_code="created",
                    target_ref=code_id,
                    details={"device_type": allowed_device_type, "agent_types": list(agent_types)},
                )
        return {
            "pairing_code": code,
            "expires_in": ttl_seconds,
            "allowed_device_type": allowed_device_type,
            "allowed_agent_types": list(agent_types),
        }

    def revoke_device(self, device_id: str) -> dict[str, Any]:
        device_id = _validate_identifier("device_id", device_id)
        psycopg = self._psycopg()
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                changed = connection.execute(
                    """
                    UPDATE devices
                    SET status = 'revoked', revoked_at = now(), auth_epoch = auth_epoch + 1, updated_at = now()
                    WHERE device_id = %s AND status <> 'revoked'
                    RETURNING tenant_id, auth_epoch
                    """,
                    (device_id,),
                ).fetchone()
                if changed is None:
                    raise AuthError("DEVICE_NOT_ACTIVE", status=404)
                connection.execute(
                    """
                    UPDATE agent_installations
                    SET status = 'revoked', auth_epoch = auth_epoch + 1, updated_at = now()
                    WHERE device_id = %s AND status <> 'revoked'
                    """,
                    (device_id,),
                )
                connection.execute(
                    "UPDATE refresh_credentials SET revoked_at = now() WHERE device_id = %s AND revoked_at IS NULL",
                    (device_id,),
                )
                _audit(
                    connection,
                    tenant_id=str(changed[0]),
                    actor_type="admin",
                    actor_id="local-admin",
                    action="auth.device.revoke",
                    result_code="revoked",
                    device_id=device_id,
                    target_ref=device_id,
                )
        return {"device_id": device_id, "status": "revoked", "auth_epoch": int(changed[1])}

    def bind_workspace(
        self,
        *,
        agent_installation_id: str,
        workspace_id: str,
        capabilities: tuple[str, ...],
    ) -> dict[str, Any]:
        """为刚登记的 Agent 追加一个明确的工作区绑定，不改写已有授权。"""

        agent_installation_id = _validate_identifier("agent_installation_id", agent_installation_id)
        workspace_id = _validate_identifier("workspace_id", workspace_id)
        capabilities = tuple(sorted({str(value).strip() for value in capabilities if str(value).strip()}))
        if not capabilities or any(len(value) > 128 or not value.replace(".", "").replace("_", "").isalnum() for value in capabilities):
            raise AuthError("WORKSPACE_CAPABILITIES_INVALID", status=400)
        psycopg = self._psycopg()
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                agent = connection.execute(
                    """
                    SELECT d.tenant_id, d.user_id, d.status, a.status
                    FROM agent_installations AS a
                    JOIN devices AS d ON d.device_id = a.device_id
                    WHERE a.agent_installation_id = %s
                    FOR UPDATE OF a, d
                    """,
                    (agent_installation_id,),
                ).fetchone()
                workspace = connection.execute(
                    """
                    SELECT tenant_id, user_id, status
                    FROM workspaces
                    WHERE workspace_id = %s
                    FOR UPDATE
                    """,
                    (workspace_id,),
                ).fetchone()
                if agent is None or workspace is None or agent[2] != "active" or agent[3] != "active":
                    raise AuthError("WORKSPACE_BINDING_OWNER_INVALID", status=400)
                if tuple(agent[:2]) != tuple(workspace[:2]) or workspace[2] != "active":
                    raise AuthError("WORKSPACE_BINDING_OWNER_INVALID", status=400)
                existing = connection.execute(
                    """
                    SELECT capabilities, status
                    FROM workspace_bindings
                    WHERE agent_installation_id = %s AND workspace_id = %s
                    FOR UPDATE
                    """,
                    (agent_installation_id, workspace_id),
                ).fetchone()
                if existing is not None:
                    if existing[1] != "active" or set(existing[0]) != set(capabilities):
                        raise AuthError("WORKSPACE_BINDING_CONFLICT", status=409)
                    status = "existing"
                else:
                    connection.execute(
                        """
                        INSERT INTO workspace_bindings (agent_installation_id, workspace_id, capabilities)
                        VALUES (%s, %s, %s)
                        """,
                        (agent_installation_id, workspace_id, list(capabilities)),
                    )
                    _audit(
                        connection,
                        tenant_id=str(agent[0]),
                        actor_type="admin",
                        actor_id=str(agent[1]),
                        action="auth.workspace.bind",
                        result_code="granted",
                        agent_installation_id=agent_installation_id,
                        target_ref=workspace_id,
                        details={"capabilities": list(capabilities)},
                    )
                    status = "granted"
        return {
            "agent_installation_id": agent_installation_id,
            "workspace_id": workspace_id,
            "capabilities": list(capabilities),
            "status": status,
        }

    def register_bootstrap_credential(self, device_id: str, credential: str) -> dict[str, Any]:
        """为 bootstrap 设备登记已安全落到本机的刷新凭据，不输出凭据值。"""

        device_id = _validate_identifier("device_id", device_id)
        credential_id, credential_hash = _parse_refresh_credential(credential)
        psycopg = self._psycopg()
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                device = connection.execute(
                    "SELECT status, auth_epoch, tenant_id FROM devices WHERE device_id = %s FOR UPDATE",
                    (device_id,),
                ).fetchone()
                if device is None or device[0] != "active":
                    raise AuthError("DEVICE_NOT_ACTIVE", status=404)
                existing = connection.execute(
                    """
                    SELECT credential_id, credential_hash, auth_epoch, revoked_at,
                           expires_at > now() AS unexpired
                    FROM refresh_credentials
                    WHERE device_id = %s
                    ORDER BY created_at DESC
                    FOR UPDATE
                    """,
                    (device_id,),
                ).fetchall()
                active = [row for row in existing if row[3] is None and row[4]]
                if active:
                    if len(active) == 1 and active[0][0] == credential_id and hmac.compare_digest(
                        str(active[0][1]), credential_hash
                    ):
                        _audit(
                            connection,
                            tenant_id=str(device[2]),
                            actor_type="admin",
                            actor_id="local-admin",
                            action="auth.bootstrap_credential.register",
                            result_code="already_registered",
                            device_id=device_id,
                            target_ref=credential_id,
                        )
                        return {
                            "device_id": device_id,
                            "credential_id": credential_id,
                            "status": "already_registered",
                        }
                    raise AuthError("ACTIVE_REFRESH_EXISTS", status=409)
                connection.execute(
                    """
                    INSERT INTO refresh_credentials (
                      credential_id, device_id, credential_hash, auth_epoch, expires_at
                    ) VALUES (%s, %s, %s, %s, now() + (%s * interval '1 day'))
                    """,
                    (credential_id, device_id, credential_hash, int(device[1]), REFRESH_TTL_DAYS),
                )
                _audit(
                    connection,
                    tenant_id=str(device[2]),
                    actor_type="admin",
                    actor_id="local-admin",
                    action="auth.bootstrap_credential.register",
                    result_code="registered",
                    device_id=device_id,
                    target_ref=credential_id,
                )
        return {"device_id": device_id, "credential_id": credential_id, "status": "registered"}

    def revoke_agent(self, agent_installation_id: str) -> dict[str, Any]:
        agent_installation_id = _validate_identifier("agent_installation_id", agent_installation_id)
        psycopg = self._psycopg()
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                changed = connection.execute(
                    """
                    UPDATE agent_installations
                    SET status = 'revoked', auth_epoch = auth_epoch + 1, updated_at = now()
                    WHERE agent_installation_id = %s AND status <> 'revoked'
                    RETURNING device_id, auth_epoch
                    """,
                    (agent_installation_id,),
                ).fetchone()
                if changed is None:
                    raise AuthError("AGENT_NOT_ACTIVE", status=404)
                tenant = connection.execute(
                    "SELECT tenant_id FROM devices WHERE device_id = %s",
                    (changed[0],),
                ).fetchone()
                _audit(
                    connection,
                    tenant_id=str(tenant[0]),
                    actor_type="admin",
                    actor_id="local-admin",
                    action="auth.agent.revoke",
                    result_code="revoked",
                    device_id=str(changed[0]),
                    agent_installation_id=agent_installation_id,
                    target_ref=agent_installation_id,
                )
        return {
            "agent_installation_id": agent_installation_id,
            "device_id": str(changed[0]),
            "status": "revoked",
            "auth_epoch": int(changed[1]),
        }


class PostgresIdentityService:
    """供 Gateway 认证端点调用；不接受请求体中的租户或用户身份。"""

    def __init__(
        self,
        dsn: str,
        signer: AccessTokenSigner,
        replay_cipher: RefreshReplayCipher,
        *,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not dsn:
            raise MetadataStoreError("缺少元数据库运行连接串")
        self._dsn = dsn
        self._signer = signer
        self._replay_cipher = replay_cipher
        self._connection_factory = connection_factory

    @staticmethod
    def _psycopg() -> Any:
        return IdentityAdmin._psycopg()

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        return self._psycopg().connect(self._dsn)

    def pair(self, payload: dict[str, Any]) -> dict[str, Any]:
        pairing_code = _validate_identifier("pairing_code", payload.get("pairing_code"))
        device_id = _validate_identifier("device_id", payload.get("device_id"))
        device_name = _validate_identifier("device_name", payload.get("device_name"))
        device_type = _validate_identifier("device_type", payload.get("device_type"))
        if device_type not in VALID_DEVICE_TYPES:
            raise AuthError("DEVICE_TYPE_INVALID", status=400)
        public_key = _validate_identifier("public_key", payload.get("public_key"))
        nonce = _validate_identifier("nonce", payload.get("nonce"))
        proof_signature = _validate_identifier("proof_signature", payload.get("proof_signature"))
        raw_agents = payload.get("agents")
        if not isinstance(raw_agents, list) or not raw_agents or len(raw_agents) > 16:
            raise AuthError("PAIR_AGENTS_INVALID", status=400)
        agents = tuple(PairingAgent.from_payload(value) for value in raw_agents)
        if len({value.agent_installation_id for value in agents}) != len(agents):
            raise AuthError("PAIR_AGENTS_INVALID", status=400)
        verify_pairing_proof(
            pairing_code=pairing_code,
            device_id=device_id,
            nonce=nonce,
            public_key=public_key,
            proof_signature=proof_signature,
        )
        credential_id, refresh_credential, credential_hash = new_refresh_credential()
        with self._connect() as connection:
            with connection.transaction():
                code = connection.execute(
                    """
                    SELECT pairing_code_id, tenant_id, user_id, allowed_device_type,
                           allowed_agent_types, expires_at > now() AS unexpired, used_at
                    FROM pairing_codes
                    WHERE code_hash = %s
                    FOR UPDATE
                    """,
                    (_hash(pairing_code),),
                ).fetchone()
                if code is None:
                    raise AuthError("PAIRING_CODE_INVALID", status=401)
                if code[6] is not None:
                    raise AuthError("PAIRING_CODE_USED", status=409)
                if not code[5]:
                    raise AuthError("PAIRING_CODE_EXPIRED", status=401)
                if code[3] != device_type:
                    raise AuthError("DEVICE_TYPE_FORBIDDEN")
                allowed_agents = set(code[4])
                if any(agent.agent_type not in allowed_agents for agent in agents):
                    raise AuthError("AGENT_TYPE_FORBIDDEN")
                existing = connection.execute(
                    "SELECT 1 FROM devices WHERE device_id = %s",
                    (device_id,),
                ).fetchone()
                if existing is not None:
                    raise AuthError("DEVICE_ID_CONFLICT", status=409)
                connection.execute(
                    """
                    INSERT INTO devices (
                      device_id, tenant_id, user_id, display_name, device_type,
                      public_key, status, paired_at, last_seen_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'active', now(), now())
                    """,
                    (device_id, code[1], code[2], device_name, device_type, public_key),
                )
                for agent in agents:
                    connection.execute(
                        """
                        INSERT INTO agent_installations (
                          agent_installation_id, device_id, agent_type, display_name, status
                        ) VALUES (%s, %s, %s, %s, 'active')
                        """,
                        (agent.agent_installation_id, device_id, agent.agent_type, agent.display_name),
                    )
                connection.execute(
                    """
                    INSERT INTO refresh_credentials (
                      credential_id, device_id, credential_hash, auth_epoch, expires_at
                    ) VALUES (%s, %s, %s, 1, now() + (%s * interval '1 day'))
                    """,
                    (credential_id, device_id, credential_hash, REFRESH_TTL_DAYS),
                )
                changed = connection.execute(
                    """
                    UPDATE pairing_codes
                    SET used_at = now(), used_by_device_id = %s
                    WHERE pairing_code_id = %s AND used_at IS NULL
                    """,
                    (device_id, code[0]),
                )
                if changed.rowcount != 1:
                    raise AuthError("PAIRING_CODE_USED", status=409)
                _audit(
                    connection,
                    tenant_id=str(code[1]),
                    actor_type="device",
                    actor_id=device_id,
                    action="auth.device.pair",
                    result_code="paired",
                    device_id=device_id,
                    target_ref=str(code[0]),
                    details={"agent_installation_ids": [value.agent_installation_id for value in agents]},
                )
        return {
            "device_id": device_id,
            "agent_installation_ids": [value.agent_installation_id for value in agents],
            "refresh_credential": refresh_credential,
            "refresh_expires_in": int(timedelta(days=REFRESH_TTL_DAYS).total_seconds()),
        }

    def refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        credential_id, presented_hash = _parse_refresh_credential(payload.get("refresh_credential"))
        agent_id = _validate_identifier("agent_installation_id", payload.get("agent_installation_id"))
        new_credential = f"{credential_id}.{secrets.token_urlsafe(32)}"
        new_hash = _hash(new_credential)
        reuse_detected = False
        replayed = False
        with self._connect() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    SELECT r.device_id, r.credential_hash, r.previous_credential_hash,
                           r.replay_until, r.expires_at > now() AS unexpired,
                           r.revoked_at, r.auth_epoch,
                           d.tenant_id, d.user_id, d.status, d.auth_epoch,
                           r.replacement_ciphertext, r.replacement_nonce,
                           r.replacement_key_version
                    FROM refresh_credentials AS r
                    JOIN devices AS d ON d.device_id = r.device_id
                    WHERE r.credential_id = %s
                    FOR UPDATE OF r, d
                    """,
                    (credential_id,),
                ).fetchone()
                if row is None or row[5] is not None or not row[4]:
                    raise AuthError("REFRESH_INVALID", status=401)
                if row[9] != "active" or int(row[6]) != int(row[10]):
                    raise AuthError("REFRESH_REVOKED", status=401)
                if hmac.compare_digest(str(row[2] or ""), presented_hash):
                    in_window = connection.execute("SELECT %s > now()", (row[3],)).fetchone()[0]
                    if in_window:
                        if row[11] is None or row[12] is None or row[13] is None:
                            raise AuthError("REFRESH_REPLAY_UNAVAILABLE", status=503)
                        try:
                            new_credential = self._replay_cipher.decrypt(
                                EncryptedRefreshCredential(bytes(row[11]), bytes(row[12]), str(row[13])),
                                credential_id=credential_id,
                            )
                        except RefreshReplayError as exc:
                            raise AuthError("REFRESH_REPLAY_UNAVAILABLE", status=503) from exc
                        replayed = True
                    else:
                        connection.execute(
                            "UPDATE refresh_credentials SET revoked_at = now() WHERE credential_id = %s",
                            (credential_id,),
                        )
                        connection.execute(
                            "UPDATE devices SET auth_epoch = auth_epoch + 1, updated_at = now() WHERE device_id = %s",
                            (row[0],),
                        )
                        connection.execute(
                            "UPDATE agent_installations SET auth_epoch = auth_epoch + 1, updated_at = now() WHERE device_id = %s",
                            (row[0],),
                        )
                        reuse_detected = True
                elif not hmac.compare_digest(str(row[1]), presented_hash):
                    raise AuthError("REFRESH_INVALID", status=401)
                if reuse_detected:
                    agent = None
                else:
                    agent = connection.execute(
                        """
                        SELECT status, auth_epoch
                        FROM agent_installations
                        WHERE agent_installation_id = %s AND device_id = %s
                        """,
                        (agent_id, row[0]),
                    ).fetchone()
                    if agent is None or agent[0] != "active":
                        raise AuthError("AGENT_FORBIDDEN")
                    if not replayed:
                        encrypted = self._replay_cipher.encrypt(new_credential, credential_id=credential_id)
                        connection.execute(
                            """
                            UPDATE refresh_credentials
                            SET credential_hash = %s,
                                previous_credential_hash = %s,
                                replay_until = now() + (%s * interval '1 second'),
                                replacement_ciphertext = %s,
                                replacement_nonce = %s,
                                replacement_key_version = %s,
                                last_used_at = now()
                            WHERE credential_id = %s
                            """,
                            (
                                new_hash,
                                row[1],
                                REFRESH_REPLAY_SECONDS,
                                encrypted.ciphertext,
                                encrypted.nonce,
                                encrypted.key_version,
                                credential_id,
                            ),
                        )
                    _audit(
                        connection,
                        tenant_id=str(row[7]),
                        actor_type="device",
                        actor_id=str(row[0]),
                        action="auth.token.refresh",
                        result_code="replayed" if replayed else "rotated",
                        device_id=str(row[0]),
                        agent_installation_id=agent_id,
                        target_ref=credential_id,
                    )
                    if not replayed:
                        connection.execute(
                            "UPDATE devices SET last_seen_at = now() WHERE device_id = %s",
                            (row[0],),
                        )
                if reuse_detected:
                    _audit(
                        connection,
                        tenant_id=str(row[7]),
                        actor_type="device",
                        actor_id=str(row[0]),
                        action="auth.token.reuse_detected",
                        result_code="revoked",
                        device_id=str(row[0]),
                        target_ref=credential_id,
                    )
            if reuse_detected:
                raise AuthError("REFRESH_REUSE_DETECTED", status=401)
        access_token, claims = self._signer.issue(
            tenant_id=str(row[7]),
            user_id=str(row[8]),
            device_id=str(row[0]),
            agent_installation_id=agent_id,
            device_auth_epoch=int(row[10]),
            agent_auth_epoch=int(agent[1]),
        )
        return {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": claims.expires_at - claims.issued_at,
            "refresh_credential": new_credential,
            "refresh_replay_window": REFRESH_REPLAY_SECONDS,
            "refresh_replayed": replayed,
        }
