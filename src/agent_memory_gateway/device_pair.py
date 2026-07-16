"""Windows 设备配对客户端。

配对码只从标准输入读取，避免进入 PowerShell 历史、命令行参数或生成的 MCP 配置。
成功后的刷新凭据只写入 Windows Credential Manager，调用方只能得到不含凭据的结果。
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib import error, request
from urllib.parse import urlsplit

from .bootstrap import VALID_AGENT_TYPES, VALID_DEVICE_TYPES
from .device_key import generate_device_key, validate_device_key_file
from .file_credential import read_file_credential, write_file_credential
from .identity_service import pairing_proof_message
from .windows_credential import read_generic_credential, write_generic_credential


class DevicePairError(RuntimeError):
    """设备无法完成安全配对。"""


@dataclass(frozen=True)
class DevicePairAgent:
    agent_installation_id: str
    agent_type: str
    display_name: str


@dataclass(frozen=True)
class DeviceIdentity:
    public_key: str
    private_key: Any


def _encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _identifier(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256:
        raise DevicePairError(f"{name.upper()}_INVALID")
    return text


def parse_agent_spec(raw: str) -> DevicePairAgent:
    """解析 ``安装实例 ID|类型|显示名``，保留显示名中的普通文字。"""

    parts = str(raw or "").split("|", 2)
    if len(parts) != 3:
        raise DevicePairError("AGENT_SPEC_INVALID")
    agent_type = _identifier("agent_type", parts[1])
    if agent_type not in VALID_AGENT_TYPES:
        raise DevicePairError("AGENT_TYPE_INVALID")
    return DevicePairAgent(
        agent_installation_id=_identifier("agent_installation_id", parts[0]),
        agent_type=agent_type,
        display_name=_identifier("agent_name", parts[2]),
    )


def load_or_create_device_identity(path: str | Path) -> DeviceIdentity:
    """读取已有设备私钥，或只在文件不存在时创建一份新的 Ed25519 私钥。"""

    key_path = Path(path)
    if not key_path.exists():
        generate_device_key(key_path)
    try:
        key_path = validate_device_key_file(key_path)
    except ValueError as exc:
        raise DevicePairError(str(exc)) from exc
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, TypeError, ValueError) as exc:
        raise DevicePairError("DEVICE_KEY_INVALID") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise DevicePairError("DEVICE_KEY_INVALID")
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return DeviceIdentity(public_key=_encode_urlsafe(public_bytes), private_key=private_key)


def _ssl_context(gateway_url: str, ca_certificate: str | Path | None) -> ssl.SSLContext | None:
    parsed = urlsplit(gateway_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise DevicePairError("GATEWAY_URL_INVALID")
    if not ca_certificate:
        return None
    if parsed.scheme != "https":
        raise DevicePairError("GATEWAY_CA_REQUIRES_HTTPS")
    path = Path(ca_certificate)
    if not path.is_file():
        raise DevicePairError("GATEWAY_CA_CERTIFICATE_MISSING")
    try:
        return ssl.create_default_context(cafile=str(path))
    except (OSError, ssl.SSLError) as exc:
        raise DevicePairError("GATEWAY_CA_CERTIFICATE_INVALID") from exc


def _request_error_code(exc: error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "PAIR_REQUEST_FAILED"
    code = str(payload.get("error") or "PAIR_REQUEST_FAILED")
    return code[:128]


def pair_device(
    *,
    gateway_url: str,
    pairing_code: str,
    device_id: str,
    device_name: str,
    device_type: str,
    device_key_file: str | Path,
    agents: Sequence[DevicePairAgent],
    credential_target: str | None,
    credential_username: str,
    credential_file: str | Path | None = None,
    ca_certificate: str | Path | None = None,
) -> dict[str, Any]:
    """向 Gateway 提交设备证明，并把刷新凭据写入受保护的本地存储。"""

    endpoint = str(gateway_url or "").rstrip("/")
    context = _ssl_context(endpoint, ca_certificate)
    pairing_code = _identifier("pairing_code", pairing_code)
    device_id = _identifier("device_id", device_id)
    device_name = _identifier("device_name", device_name)
    device_type = _identifier("device_type", device_type)
    credential_username = _identifier("credential_username", credential_username)
    if device_type not in VALID_DEVICE_TYPES:
        raise DevicePairError("DEVICE_TYPE_INVALID")
    if not agents or len(agents) > 16:
        raise DevicePairError("PAIR_AGENTS_INVALID")
    if len({agent.agent_installation_id for agent in agents}) != len(agents):
        raise DevicePairError("PAIR_AGENTS_INVALID")
    credential_target = str(credential_target or "").strip()
    if bool(credential_target) == bool(credential_file):
        raise DevicePairError("REFRESH_CREDENTIAL_STORAGE_INVALID")
    if credential_target:
        credential_target = _identifier("credential_target", credential_target)
        if read_generic_credential(credential_target) is not None:
            raise FileExistsError(f"拒绝覆盖已有 Windows 凭据：{credential_target}")
    elif read_file_credential(credential_file) is not None:
        raise FileExistsError(f"拒绝覆盖已有刷新凭据文件：{credential_file}")

    identity = load_or_create_device_identity(device_key_file)
    nonce = _encode_urlsafe(secrets.token_bytes(24))
    signature = _encode_urlsafe(identity.private_key.sign(pairing_proof_message(pairing_code, device_id, nonce)))
    payload = {
        "pairing_code": pairing_code,
        "device_id": device_id,
        "device_name": device_name,
        "device_type": device_type,
        "public_key": identity.public_key,
        "nonce": nonce,
        "proof_signature": signature,
        "agents": [
            {
                "agent_installation_id": agent.agent_installation_id,
                "agent_type": agent.agent_type,
                "display_name": agent.display_name,
            }
            for agent in agents
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = request.Request(
        endpoint + "/v1/auth/pair",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15, context=context) as response:  # noqa: S310
            result = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise DevicePairError(_request_error_code(exc)) from None
    except (error.URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError):
        raise DevicePairError("GATEWAY_UNAVAILABLE") from None
    if not isinstance(result, dict):
        raise DevicePairError("PAIR_RESPONSE_INVALID")

    refresh_credential = str(result.get("refresh_credential") or "")
    paired_device_id = str(result.get("device_id") or "")
    paired_agents = result.get("agent_installation_ids")
    expected_agents = [agent.agent_installation_id for agent in agents]
    if (
        not refresh_credential
        or len(refresh_credential) > 512
        or paired_device_id != device_id
        or not isinstance(paired_agents, list)
        or paired_agents != expected_agents
    ):
        raise DevicePairError("PAIR_RESPONSE_INVALID")

    if credential_target:
        write_generic_credential(credential_target, credential_username, refresh_credential)
    else:
        write_file_credential(credential_file, credential_username, refresh_credential)
    result = {
        "status": "paired",
        "device_id": device_id,
        "agent_installation_ids": expected_agents,
    }
    if credential_target:
        result["credential_target"] = credential_target
    else:
        result["credential_file"] = str(credential_file)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="使用一次性配对码登记设备，不在命令行保存配对码或刷新凭据")
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--pairing-code-stdin", action="store_true", help="仅从标准输入读取一次性配对码")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--device-name", required=True)
    parser.add_argument("--device-type", choices=sorted(VALID_DEVICE_TYPES), required=True)
    parser.add_argument("--device-key-file", type=Path, required=True)
    parser.add_argument("--agent", action="append", default=[], help="安装实例 ID|类型|显示名；可重复传入")
    parser.add_argument("--credential-target")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--credential-username", required=True)
    parser.add_argument("--gateway-ca-certificate", type=Path)
    args = parser.parse_args(argv)
    if not args.pairing_code_stdin:
        parser.error("配对码只能通过 --pairing-code-stdin 从标准输入读取")
    pairing_code = sys.stdin.readline().rstrip("\r\n")
    if not pairing_code:
        parser.error("未读取到配对码")
    try:
        agents = [parse_agent_spec(value) for value in args.agent]
        result = pair_device(
            gateway_url=args.gateway_url,
            pairing_code=pairing_code,
            device_id=args.device_id,
            device_name=args.device_name,
            device_type=args.device_type,
            device_key_file=args.device_key_file,
            agents=agents,
            credential_target=args.credential_target,
            credential_username=args.credential_username,
            credential_file=args.credential_file,
            ca_certificate=args.gateway_ca_certificate,
        )
    finally:
        pairing_code = ""
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
