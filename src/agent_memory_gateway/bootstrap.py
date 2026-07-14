"""管理员显式登记初始设备、Agent 与工作区绑定。"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .metadata_store import MetadataStoreError


VALID_DEVICE_TYPES = frozenset({"windows", "nas", "other"})
VALID_AGENT_TYPES = frozenset({"codex", "hermes", "other"})


@dataclass(frozen=True)
class BootstrapSpec:
    tenant_id: str
    user_id: str
    user_name: str
    device_id: str
    device_name: str
    device_type: str
    device_public_key: str
    agent_installation_id: str
    agent_name: str
    agent_type: str
    workspace_id: str
    workspace_name: str
    capabilities: tuple[str, ...]

    def validate(self) -> None:
        for field in (
            "tenant_id",
            "user_id",
            "user_name",
            "device_id",
            "device_name",
            "device_public_key",
            "agent_installation_id",
            "agent_name",
            "workspace_id",
            "workspace_name",
        ):
            value = str(getattr(self, field)).strip()
            if not value or len(value) > 256:
                raise ValueError(f"{field} 无效")
        if self.device_type not in VALID_DEVICE_TYPES:
            raise ValueError("device_type 无效")
        if self.agent_type not in VALID_AGENT_TYPES:
            raise ValueError("agent_type 无效")
        if not self.capabilities or any(not value.strip() for value in self.capabilities):
            raise ValueError("capabilities 不能为空")


class MetadataBootstrap:
    """只允许追加相容的初始绑定，拒绝覆盖已登记的所有权或公钥。"""

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

    def register(self, spec: BootstrapSpec) -> dict[str, str]:
        spec.validate()
        psycopg = self._psycopg()
        with psycopg.connect(self._dsn) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO tenants (tenant_id, display_name)
                    VALUES (%s, %s)
                    ON CONFLICT (tenant_id) DO NOTHING
                    """,
                    (spec.tenant_id, spec.tenant_id),
                )
                self._ensure_user(connection, spec)
                self._ensure_device(connection, spec)
                self._ensure_agent(connection, spec)
                self._ensure_workspace(connection, spec)
                self._ensure_binding(connection, spec)
        return {
            "tenant_id": spec.tenant_id,
            "device_id": spec.device_id,
            "agent_installation_id": spec.agent_installation_id,
            "workspace_id": spec.workspace_id,
            "status": "registered",
        }

    @staticmethod
    def _ensure_user(connection: Any, spec: BootstrapSpec) -> None:
        connection.execute(
            """
            INSERT INTO users (user_id, tenant_id, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (spec.user_id, spec.tenant_id, spec.user_name),
        )
        row = connection.execute("SELECT tenant_id, status FROM users WHERE user_id = %s", (spec.user_id,)).fetchone()
        if row is None or row[0] != spec.tenant_id or row[1] != "active":
            raise MetadataStoreError("用户已被其他租户占用或未激活")

    @staticmethod
    def _ensure_device(connection: Any, spec: BootstrapSpec) -> None:
        connection.execute(
            """
            INSERT INTO devices (device_id, tenant_id, user_id, display_name, device_type, public_key, status, paired_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'active', now())
            ON CONFLICT (device_id) DO NOTHING
            """,
            (
                spec.device_id,
                spec.tenant_id,
                spec.user_id,
                spec.device_name,
                spec.device_type,
                spec.device_public_key,
            ),
        )
        row = connection.execute(
            "SELECT tenant_id, user_id, public_key, status FROM devices WHERE device_id = %s",
            (spec.device_id,),
        ).fetchone()
        if row is None or tuple(row[:3]) != (spec.tenant_id, spec.user_id, spec.device_public_key) or row[3] != "active":
            raise MetadataStoreError("设备已被其他主体占用、公钥不一致或未激活")

    @staticmethod
    def _ensure_agent(connection: Any, spec: BootstrapSpec) -> None:
        connection.execute(
            """
            INSERT INTO agent_installations (agent_installation_id, device_id, agent_type, display_name, status)
            VALUES (%s, %s, %s, %s, 'active')
            ON CONFLICT (agent_installation_id) DO NOTHING
            """,
            (spec.agent_installation_id, spec.device_id, spec.agent_type, spec.agent_name),
        )
        row = connection.execute(
            "SELECT device_id, status FROM agent_installations WHERE agent_installation_id = %s",
            (spec.agent_installation_id,),
        ).fetchone()
        if row is None or row[0] != spec.device_id or row[1] != "active":
            raise MetadataStoreError("Agent 安装实例已绑定到其他设备或未激活")

    @staticmethod
    def _ensure_workspace(connection: Any, spec: BootstrapSpec) -> None:
        connection.execute(
            """
            INSERT INTO workspaces (workspace_id, tenant_id, user_id, display_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            (spec.workspace_id, spec.tenant_id, spec.user_id, spec.workspace_name),
        )
        row = connection.execute(
            "SELECT tenant_id, user_id, status FROM workspaces WHERE workspace_id = %s",
            (spec.workspace_id,),
        ).fetchone()
        if row is None or tuple(row[:2]) != (spec.tenant_id, spec.user_id) or row[2] != "active":
            raise MetadataStoreError("工作区已被其他主体占用或未激活")

    @staticmethod
    def _ensure_binding(connection: Any, spec: BootstrapSpec) -> None:
        connection.execute(
            """
            INSERT INTO workspace_bindings (agent_installation_id, workspace_id, capabilities)
            VALUES (%s, %s, %s)
            ON CONFLICT (agent_installation_id, workspace_id) DO NOTHING
            """,
            (spec.agent_installation_id, spec.workspace_id, list(spec.capabilities)),
        )
        row = connection.execute(
            """
            SELECT capabilities, status
            FROM workspace_bindings
            WHERE agent_installation_id = %s AND workspace_id = %s
            """,
            (spec.agent_installation_id, spec.workspace_id),
        ).fetchone()
        if row is None or row[1] != "active" or not set(spec.capabilities).issubset(set(row[0])):
            raise MetadataStoreError("工作区绑定能力不一致或未激活")


def _split_capabilities(raw: str) -> tuple[str, ...]:
    values = tuple(sorted({value.strip() for value in raw.split(",") if value.strip()}))
    if not values:
        raise ValueError("至少提供一个 capability")
    return values


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="管理员显式登记初始设备、Agent 与工作区")
    parser.add_argument("--metadata-dsn", default=os.environ.get("MEMORY_METADATA_MIGRATOR_DSN"))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--user-name", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--device-name", required=True)
    parser.add_argument("--device-type", choices=sorted(VALID_DEVICE_TYPES), required=True)
    parser.add_argument("--device-public-key", required=True)
    parser.add_argument("--agent-installation-id", required=True)
    parser.add_argument("--agent-name", required=True)
    parser.add_argument("--agent-type", choices=sorted(VALID_AGENT_TYPES), required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--capabilities", required=True, help="逗号分隔，例如 memory.write_event,memory.search")
    args = parser.parse_args(argv)
    if not args.metadata_dsn:
        parser.error("需要 --metadata-dsn 或 MEMORY_METADATA_MIGRATOR_DSN")
    spec = BootstrapSpec(
        tenant_id=args.tenant_id,
        user_id=args.user_id,
        user_name=args.user_name,
        device_id=args.device_id,
        device_name=args.device_name,
        device_type=args.device_type,
        device_public_key=args.device_public_key,
        agent_installation_id=args.agent_installation_id,
        agent_name=args.agent_name,
        agent_type=args.agent_type,
        workspace_id=args.workspace_id,
        workspace_name=args.workspace_name,
        capabilities=_split_capabilities(args.capabilities),
    )
    print(json.dumps(MetadataBootstrap(args.metadata_dsn).register(spec), ensure_ascii=False, indent=2))
