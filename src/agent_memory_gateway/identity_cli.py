"""管理员身份管理 CLI。"""

from __future__ import annotations

import argparse
import json
import os
from typing import Sequence

from .bootstrap import VALID_AGENT_TYPES, VALID_DEVICE_TYPES
from .identity_service import IdentityAdmin, PAIRING_TTL_SECONDS, new_refresh_credential
from .windows_credential import read_generic_credential, write_generic_credential


def _agent_types(raw: str) -> tuple[str, ...]:
    values = tuple(sorted({value.strip() for value in raw.split(",") if value.strip()}))
    if not values or any(value not in VALID_AGENT_TYPES for value in values):
        raise argparse.ArgumentTypeError("Agent 类型必须是 codex、hermes、other 中的一个或多个")
    return values


def _capabilities(raw: str) -> tuple[str, ...]:
    values = tuple(sorted({value.strip() for value in raw.split(",") if value.strip()}))
    if not values:
        raise argparse.ArgumentTypeError("至少提供一个 capability")
    return values


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="管理员设备配对和撤销")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_metadata_dsn(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--metadata-dsn",
            default=os.environ.get("MEMORY_METADATA_MIGRATOR_DSN"),
            help="默认读取 MEMORY_METADATA_MIGRATOR_DSN",
        )

    pairing = commands.add_parser("pairing-code", help="生成十分钟内一次性使用的设备配对码")
    add_metadata_dsn(pairing)
    pairing.add_argument("--tenant-id", required=True)
    pairing.add_argument("--user-id", required=True)
    pairing.add_argument("--device-type", choices=sorted(VALID_DEVICE_TYPES), required=True)
    pairing.add_argument("--agent-types", type=_agent_types, required=True, help="逗号分隔")
    pairing.add_argument("--ttl-seconds", type=int, default=PAIRING_TTL_SECONDS)

    revoke_device = commands.add_parser("revoke-device", help="撤销设备、刷新凭据和该设备全部 Agent")
    add_metadata_dsn(revoke_device)
    revoke_device.add_argument("--device-id", required=True)

    revoke_agent = commands.add_parser("revoke-agent", help="仅撤销一个 Agent 安装实例")
    add_metadata_dsn(revoke_agent)
    revoke_agent.add_argument("--agent-installation-id", required=True)

    bind_workspace = commands.add_parser("bind-workspace", help="为已登记 Agent 追加最小权限工作区绑定")
    add_metadata_dsn(bind_workspace)
    bind_workspace.add_argument("--agent-installation-id", required=True)
    bind_workspace.add_argument("--workspace-id", required=True)
    bind_workspace.add_argument("--capabilities", type=_capabilities, required=True, help="逗号分隔")

    bootstrap_credential = commands.add_parser(
        "bootstrap-credential",
        help="为已 bootstrap 的 Windows 设备安全登记首个刷新凭据",
    )
    add_metadata_dsn(bootstrap_credential)
    bootstrap_credential.add_argument("--device-id", required=True)
    bootstrap_credential.add_argument("--credential-target", required=True)
    bootstrap_credential.add_argument("--username", required=True)

    args = parser.parse_args(argv)
    if not args.metadata_dsn:
        parser.error("需要 --metadata-dsn 或 MEMORY_METADATA_MIGRATOR_DSN")
    admin = IdentityAdmin(args.metadata_dsn)
    if args.command == "pairing-code":
        result = admin.create_pairing_code(
            tenant_id=args.tenant_id,
            user_id=args.user_id,
            allowed_device_type=args.device_type,
            allowed_agent_types=args.agent_types,
            ttl_seconds=args.ttl_seconds,
        )
    elif args.command == "revoke-device":
        result = admin.revoke_device(args.device_id)
    elif args.command == "revoke-agent":
        result = admin.revoke_agent(args.agent_installation_id)
    elif args.command == "bind-workspace":
        result = admin.bind_workspace(
            agent_installation_id=args.agent_installation_id,
            workspace_id=args.workspace_id,
            capabilities=args.capabilities,
        )
    else:
        saved = read_generic_credential(args.credential_target)
        if saved is None:
            _, credential, _ = new_refresh_credential()
            write_generic_credential(args.credential_target, args.username, credential)
            credential_source = "created"
        else:
            saved_username, credential = saved
            if saved_username != args.username:
                parser.error("已有 Windows 凭据的 username 不一致，拒绝复用或覆盖")
            credential_source = "existing"
        result = admin.register_bootstrap_credential(args.device_id, credential)
        result["credential_target"] = args.credential_target
        result["credential_source"] = credential_source
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
