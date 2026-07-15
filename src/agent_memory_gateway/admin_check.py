"""通过本机 Sidecar 执行只读运行检查。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

from .sidecar_daemon import SidecarDaemonError, get_shared_sidecar


def _workspace_id(value: str | None) -> str:
    workspace_id = str(value or os.environ.get("MEMORY_DEFAULT_WORKSPACE") or "").strip()
    if not workspace_id:
        raise ValueError("WORKSPACE_ID_REQUIRED")
    return workspace_id


def _heartbeat_age_seconds(value: Any, now: datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())


def evaluate_overview(
    overview: dict[str, Any], *, max_heartbeat_age_seconds: int, now: datetime | None = None
) -> dict[str, Any]:
    """把只读概览变成可供计划任务或监控读取的稳定结果。"""

    current_time = now or datetime.now(timezone.utc)
    counts = overview.get("counts") if isinstance(overview.get("counts"), dict) else {}
    heartbeat_age = _heartbeat_age_seconds(overview.get("worker_heartbeat_at"), current_time)
    problems: list[str] = []
    if heartbeat_age is None or heartbeat_age > max_heartbeat_age_seconds:
        problems.append("WORKER_HEARTBEAT_STALE")
    if int(counts.get("retryable_events") or 0) > 0:
        problems.append("RETRYABLE_EVENTS_PRESENT")
    if int(counts.get("unresolved_dead_letters") or 0) > 0:
        problems.append("DEAD_LETTERS_PRESENT")
    return {
        "ok": not problems,
        "workspace_id": str(overview.get("workspace_id") or ""),
        "checked_at": current_time.isoformat(),
        "worker_heartbeat_at": overview.get("worker_heartbeat_at"),
        "worker_heartbeat_age_seconds": heartbeat_age,
        "counts": {
            "pending_reviews": int(counts.get("pending_reviews") or 0),
            "retryable_events": int(counts.get("retryable_events") or 0),
            "unresolved_dead_letters": int(counts.get("unresolved_dead_letters") or 0),
            "active_devices": int(counts.get("active_devices") or 0),
        },
        "problems": problems,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="检查共享记忆 Gateway 的只读运行状态")
    parser.add_argument("--workspace")
    parser.add_argument("--max-heartbeat-age-seconds", type=int, default=90)
    args = parser.parse_args()
    if not 1 <= args.max_heartbeat_age_seconds <= 3600:
        raise SystemExit("MAX_HEARTBEAT_AGE_INVALID")
    try:
        overview = get_shared_sidecar().admin_overview(
            {"workspace_id": _workspace_id(args.workspace)}
        )
        result = evaluate_overview(
            overview, max_heartbeat_age_seconds=args.max_heartbeat_age_seconds
        )
    except (SidecarDaemonError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
