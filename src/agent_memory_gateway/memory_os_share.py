"""Memory OS → Agent Memory Gateway 单向自动推送（上游版）。

把本机 Memory OS（SQLite 记忆库）中已入池的高置信记忆推送到共享
Gateway，让绑定在同一工作区的其他 Agent（Codex / OpenClaw / Hermes）
也能检索到核心偏好、决策与事实。这是本机记忆增强系统向外共享的
唯一出口，也是 Gateway 外部源白名单通道（capture_mode=
automatic_whitelist）的官方客户端实现。

设计约束（KISS / YAGNI）：
- 单向：只推不拉。共享中枢不是本机记忆的来源（合并检索另由调用方实现）。
- 门控：只推 active + confidence >= 0.8 + 白名单类型的记忆；
  敏感内容用与 Gateway 相同的 SensitiveContentScanner 预筛，
  Gateway 端会再次独立扫描，双保险。
- 幂等：event_id = mos_{memory_id} 确定性生成；本机 share_log 表 +
  Gateway external_memory_bindings + gateway_events ON CONFLICT 三重去重，
  重复调用不会产生重复条目。内容变更会自动生成新 source_revision 重新绑定。
- 回执：推送入队与中枢确认分开记录。入队时 share_log 记 queued/pending，
  下一轮 share_sync 先对账（sync 回执中 applied/duplicate 回写为终态），
  不以 pending 数量代替"中枢已接收"。
- 直通：evidence=user_explicit 走 Gateway 确认写入通道，不堆积审核队列。
  依据：这些记忆已经过本机蒸馏、复习与治理三层门控。
- 降级：共享 Sidecar 不可用时返回 skipped，不中断调用方。
- 自包含：不依赖 Memory OS 运行时的私有模块，数据库路径与 Sidecar
  凭据位置由调用方注入，方便在本仓库内直接单元测试。

部署说明：本文件是上游源码；HermesData/agent-memory/memory_os_share.py
是部署副本（接线本机 Memory OS 的 maintenance 管线），二者内容保持
一致，后续改进请先在本文档修改并经 PR 审核后再同步部署副本。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_memory_gateway.security import SensitiveContentScanner
from agent_memory_gateway.sidecar_daemon import LocalSidecarProxy, daemon_auth_token

DEFAULT_WORKSPACE = "agent-memory-gateway"
DEFAULT_SIDECAR_URL = "http://127.0.0.1:8766"

# 推送类型白名单：只共享"知识"，不共享过程轨迹与事件片段。
SHARE_TYPES = frozenset(
    {"preference", "decision", "fact", "environment", "procedural", "constraint", "insight"}
)

# 本机类型 → Gateway kind（影响 Gateway 侧半衰期）。
KIND_MAP = {
    "preference": "preference",
    "constraint": "preference",
    "decision": "preference",
    "procedural": "procedure",
    "environment": "device_fact",
    "fact": "fact",
    "insight": "fact",
}

_PROVIDER_TYPE = "memory-os"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.@:-]{1,128}\Z")

_SHARE_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS share_log (
  memory_id INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  pushed_at TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _candidates(connection: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """返回满足门控且未推送过的候选记忆。

    共享策略门控：只推送显式声明 share_policy='shared' 的记忆；
    private（默认）与 sensitive 永不出本机。
    """
    placeholders = ",".join("?" for _ in SHARE_TYPES)
    return connection.execute(
        f"""
        SELECT m.id, m.memory_type, m.content, m.confidence
        FROM memories AS m
        LEFT JOIN share_log AS s ON s.memory_id = m.id
        WHERE m.status = 'active'
          AND m.confidence >= 0.8
          AND m.memory_type IN ({placeholders})
          AND COALESCE(m.share_policy, 'private') = 'shared'
          AND s.memory_id IS NULL
        ORDER BY m.id DESC
        LIMIT ?
        """,
        (*sorted(SHARE_TYPES), int(limit)),
    ).fetchall()


def _sensitive_skip(content: str, scanner: SensitiveContentScanner) -> bool:
    """复用 Gateway 的敏感内容扫描器预筛；异常时保守放行（Gateway 端仍会拦截）。"""
    try:
        return bool(scanner.assess((content,)).has_sensitive_content)
    except Exception:
        return False


def _payload(
    memory_id: int,
    memory_type: str,
    content: str,
    confidence: float,
    provider_instance_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "event_id": f"mos_{memory_id}",
        "workspace_id": workspace_id,
        "content": content,
        "scope": "workspace",
        "kind": KIND_MAP.get(memory_type, "fact"),
        "evidence": "user_explicit",
        "confidence": round(float(confidence), 4),
        "metadata": {
            "provenance": {
                "provider_type": _PROVIDER_TYPE,
                "provider_instance_id": provider_instance_id,
                "source_record_id": str(memory_id),
                "source_revision": revision,
                "capture_mode": "automatic_whitelist",
            }
        },
    }


def _mark(
    connection: sqlite3.Connection,
    memory_id: int,
    event_id: str,
    status: str,
    detail: str,
) -> None:
    connection.execute(
        """
        INSERT INTO share_log (memory_id, event_id, status, detail, pushed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (memory_id) DO UPDATE SET
          event_id = excluded.event_id,
          status = excluded.status,
          detail = excluded.detail,
          pushed_at = excluded.pushed_at
        """,
        (int(memory_id), event_id, status, str(detail)[:200], _now()),
    )


def _sidecar_proxy(
    sidecar_url: str,
    sidecar_env_path: Path,
    agent_installation_id: str,
) -> LocalSidecarProxy | None:
    """从 Sidecar 环境文件派生 RPC 代理；任一环节失败返回 None（降级）。"""
    if not sidecar_env_path.is_file():
        return None
    values: dict[str, str] = {}
    for line in sidecar_env_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.+)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    try:
        token = daemon_auth_token(values.get("MEMORY_OUTBOX_KEY", ""))
    except Exception:
        return None
    proxy = LocalSidecarProxy(sidecar_url, token, agent_installation_id=agent_installation_id)
    return proxy if proxy.health() else None


_CONFIRMED_STATUSES = frozenset({"applied", "duplicate", "source_duplicate"})


def reconcile_receipts(
    connection: sqlite3.Connection,
    proxy: Any,
    workspace_id: str,
) -> dict[str, Any]:
    """把在途推送的最终回执回写 share_log，返回对账统计。

    只处理非终态记录（pending/queued）：调用 sync 拿回执，
    applied/duplicate 视为中枢已确认；仍无回执的保持原状等下一轮。
    """

    in_flight = connection.execute(
        """
        SELECT memory_id, event_id FROM share_log
        WHERE status IN ('pending', 'queued')
        ORDER BY memory_id
        """
    ).fetchall()
    if not in_flight:
        return {"confirmed": 0, "pending_in_flight": 0, "receipts_available": True}

    receipts_available = False
    receipts: dict[str, str] = {}
    try:
        sync_result = proxy.sync(workspace_id) or {}
        for item in sync_result.get("receipts", []) or []:
            if isinstance(item, dict) and item.get("event_id"):
                receipts[str(item["event_id"])] = str(item.get("status") or "pending")
                receipts_available = True
    except Exception:
        pass

    confirmed = 0
    still_pending = 0
    for row in in_flight:
        event_id = str(row["event_id"])
        final_status = receipts.get(event_id)
        if final_status in _CONFIRMED_STATUSES:
            _mark(connection, int(row["memory_id"]), event_id, final_status, "")
            confirmed += 1
        else:
            still_pending += 1
    connection.commit()
    return {
        "confirmed": confirmed,
        "pending_in_flight": still_pending,
        "receipts_available": receipts_available,
    }


def share_sync(
    db_path: str | Path,
    *,
    sidecar_url: str = DEFAULT_SIDECAR_URL,
    sidecar_env_path: str | Path | None = None,
    agent_installation_id: str = "",
    provider_instance_id: str = "",
    workspace_id: str = DEFAULT_WORKSPACE,
    limit: int = 20,
    dry_run: bool = False,
    proxy: Any | None = None,
) -> dict[str, Any]:
    """把 Memory OS 高置信记忆推送到共享 Gateway；返回统计结果。

    流程分两阶段：先对账上一轮在途推送的最终回执（reconciled），
    再推送新的候选（pushed 表示入队成功，最终确认见 reconciled）。
    proxy 参数用于测试注入；为 None 时按 sidecar_env_path 派生真实代理。
    """

    limit = max(1, min(int(limit), 50))
    db = Path(db_path)
    provider_instance_id = str(provider_instance_id).strip() or (
        "memory-os-" + str(hashlib.sha256(str(agent_installation_id).encode("utf-8")).hexdigest())[:16]
    )
    connection = _connect(db)
    connection.execute(_SHARE_LOG_TABLE)
    connection.commit()

    candidates = _candidates(connection, limit)
    if dry_run:
        connection.close()
        return {
            "success": True,
            "dry_run": True,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "memory_id": row["id"],
                    "type": row["memory_type"],
                    "confidence": round(float(row["confidence"]), 3),
                    "content": str(row["content"])[:80],
                }
                for row in candidates
            ],
            "pushed": 0,
            "skipped": 0,
            "failed": 0,
        }

    if proxy is None:
        env_path = Path(sidecar_env_path) if sidecar_env_path else None
        if env_path is None:
            connection.close()
            return {
                "success": True,
                "shared_available": False,
                "candidate_count": len(candidates),
                "pushed": 0,
                "skipped": len(candidates),
                "note": "未提供 Sidecar 环境文件，本次跳过",
            }
        proxy = _sidecar_proxy(sidecar_url, env_path, agent_installation_id)
    if proxy is None:
        connection.close()
        return {
            "success": True,
            "shared_available": False,
            "candidate_count": len(candidates),
            "pushed": 0,
            "skipped": len(candidates),
            "note": "共享 Sidecar 不可用，本次跳过",
        }

    scanner = SensitiveContentScanner()
    reconcile = reconcile_receipts(connection, proxy, workspace_id)
    pushed, skipped, failed = 0, 0, 0
    details: list[dict[str, Any]] = []
    for row in candidates:
        memory_id, memory_type = int(row["id"]), str(row["memory_type"])
        content = str(row["content"]).strip()
        if _sensitive_skip(content, scanner):
            skipped += 1
            _mark(connection, memory_id, f"mos_{memory_id}", "skipped", "sensitive")
            details.append({"memory_id": memory_id, "status": "skipped", "reason": "sensitive"})
            continue
        payload = _payload(
            memory_id, memory_type, content, float(row["confidence"]),
            provider_instance_id, workspace_id,
        )
        try:
            result = proxy.remember(payload)
        except Exception as exc:
            failed += 1
            _mark(connection, memory_id, payload["event_id"], "failed", type(exc).__name__)
            details.append(
                {"memory_id": memory_id, "status": "failed", "reason": type(exc).__name__}
            )
            continue
        status = str(result.get("status") or "unknown")
        error = str(result.get("error") or "")
        if status in {"applied", "duplicate", "source_duplicate", "queued", "pending"}:
            pushed += 1  # 入队成功；最终确认由下一轮 reconcile 回写
        else:
            failed += 1
        _mark(connection, memory_id, payload["event_id"], status, error)
        details.append({"memory_id": memory_id, "status": status, "error": error})

    connection.commit()
    connection.close()
    return {
        "success": True,
        "shared_available": True,
        "candidate_count": len(candidates),
        "pushed": pushed,
        "skipped": skipped,
        "failed": failed,
        "reconciled": reconcile,
        "details": details,
    }


def forget_shared(
    db_path: str | Path,
    memory_id: int,
    content: str,
    *,
    sidecar_url: str = DEFAULT_SIDECAR_URL,
    sidecar_env_path: str | Path | None = None,
    agent_installation_id: str = "",
    workspace_id: str = DEFAULT_WORKSPACE,
    proxy: Any | None = None,
) -> dict[str, Any]:
    """本机遗忘后尽力撤销远端副本；失败不影响本机遗忘（返回 remote 状态）。

    定位远端条目的方式：按推送原文在 Gateway 检索结果中精确匹配
    content，命中即拿到 backend_ref（Gateway 侧 memory_id），再调
    forget 归档。查不到时返回 not_located，调用方可提示人工撤销。
    """

    db = Path(db_path)
    connection = _connect(db)
    row = connection.execute(
        "SELECT event_id, status FROM share_log WHERE memory_id = ?",
        (int(memory_id),),
    ).fetchone()
    if row is None:
        connection.close()
        return {"remote": "not_shared", "memory_id": int(memory_id)}
    event_id = str(row["event_id"])
    if str(row["status"]) == "forgotten":
        connection.close()
        return {"remote": "already_forgotten", "memory_id": int(memory_id)}

    if proxy is None:
        env_path = Path(sidecar_env_path) if sidecar_env_path else None
        if env_path is None:
            connection.close()
            return {"remote": "unavailable", "hint": "未提供 Sidecar 环境文件"}
        proxy = _sidecar_proxy(sidecar_url, env_path, agent_installation_id)
    if proxy is None:
        connection.close()
        return {"remote": "unavailable", "hint": "共享 Sidecar 不可用"}

    target: str | None = None
    try:
        result = proxy.search(
            {"query": str(content)[:120], "limit": 20, "workspace_id": workspace_id}
        )
        for item in result.get("memories", []) or []:
            if str(item.get("content") or "").strip() == str(content).strip():
                target = str(item.get("memory_id") or "")
                break
    except Exception:
        target = None
    if not target:
        connection.close()
        return {"remote": "not_located", "hint": "远端未定位到原文，可手动用 memory_forget 撤销"}

    try:
        gateway = proxy.forget({"workspace_id": workspace_id, "memory_id": target})
    except Exception as exc:
        connection.close()
        return {"remote": "forget_failed", "error": type(exc).__name__}
    _mark(connection, int(memory_id), event_id, "forgotten", str(target))
    connection.commit()
    connection.close()
    return {"remote": "archived", "backend_ref": target, "gateway": gateway}
