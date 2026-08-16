import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agent_memory_gateway.memory_os_share import (
    _SHARE_LOG_TABLE,
    _candidates,
    _payload,
    _sensitive_skip,
    share_sync,
)
from agent_memory_gateway.security import SensitiveContentScanner


def _cleanup_tmp(tmp: tempfile.TemporaryDirectory) -> None:
    """Windows 上 sqlite 句柄释放有延迟，重试几次避免 PermissionError。"""
    for _ in range(5):
        try:
            tmp.cleanup()
            return
        except PermissionError:
            time.sleep(0.05)

_MEMORIES_SCHEMA = """
CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_type TEXT NOT NULL,
  content TEXT NOT NULL,
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  share_policy TEXT NOT NULL DEFAULT 'private'
)
"""


class FakeSidecar:
    """最小 RPC 代理桩：记录 remember 调用，sync 返回可配置回执。"""

    def __init__(self, status: str = "applied"):
        self.remembered: list[dict] = []
        self.status = status
        self.receipts: dict[str, str] = {}

    def remember(self, payload):
        self.remembered.append(payload)
        return {"status": self.status, "event_id": payload["event_id"]}

    def sync(self, workspace_id):
        items = [
            {"event_id": event_id, "status": status}
            for event_id, status in self.receipts.items()
        ]
        return {"receipts": items, "workspaces": [workspace_id]}


def _make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_MEMORIES_SCHEMA)
    connection.execute(_SHARE_LOG_TABLE)
    connection.commit()
    connection.close()


def _seed(
    path: Path,
    memories: list[tuple[str, str, float, str]],
) -> None:
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO memories (memory_type, content, confidence, status) VALUES (?, ?, ?, ?)",
        memories,
    )
    connection.commit()
    connection.close()


def _seed_full(
    path: Path,
    memories: list[tuple[str, str, float, str, str]],
) -> None:
    connection = sqlite3.connect(path)
    connection.executemany(
        "INSERT INTO memories (memory_type, content, confidence, status, share_policy) VALUES (?, ?, ?, ?, ?)",
        memories,
    )
    connection.commit()
    connection.close()


def _mark_shared(path: Path, memory_ids: list[int]) -> None:
    connection = sqlite3.connect(path)
    connection.executemany(
        "UPDATE memories SET share_policy = 'shared' WHERE id = ?",
        [(mid,) for mid in memory_ids],
    )
    connection.commit()
    connection.close()


class CandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "memory.db"
        _make_db(self.db_path)
        _seed(
            self.db_path,
            [
                ("preference", "开发工具装到 E 盘", 0.9, "active"),
                ("fact", "NAS 地址 192.168.100.144", 0.85, "active"),
                ("preference", "低置信偏好", 0.5, "active"),
                ("trajectory", "过程轨迹不应共享", 0.95, "active"),
                ("preference", "已归档偏好", 0.9, "archived"),
            ],
        )

    def tearDown(self):
        _cleanup_tmp(self.tmp)

    def test_candidates_respect_gates(self):
        _mark_shared(self.db_path, [1, 2])
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        rows = _candidates(connection, 20)
        connection.close()
        ids = [int(row["id"]) for row in rows]
        # 只留下 shared + conf>=0.8 + 白名单类型 + active 的两条
        self.assertEqual(sorted(ids), [1, 2])

    def test_private_memories_are_excluded(self):
        _mark_shared(self.db_path, [1])
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        rows = _candidates(connection, 20)
        connection.close()
        ids = [int(row["id"]) for row in rows]
        # 仅显式 shared 的 #1 进候选，private 的 #2 被排除
        self.assertEqual(ids, [1])

    def test_pushed_memories_are_excluded(self):
        _mark_shared(self.db_path, [1, 2])
        share_sync(
            self.db_path,
            proxy=FakeSidecar(),
            agent_installation_id="test-agent",
            workspace_id="test-workspace",
            limit=20,
        )
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        rows = _candidates(connection, 20)
        connection.close()
        self.assertEqual(rows, [])


class SensitiveSkipTests(unittest.TestCase):
    def setUp(self):
        self.scanner = SensitiveContentScanner()

    def test_credential_content_is_flagged(self):
        content = "数据库连接串包含 password=not-for-memory 请勿保存"
        self.assertTrue(_sensitive_skip(content, self.scanner))

    def test_normal_content_passes(self):
        self.assertFalse(_sensitive_skip("用户偏好：开发工具安装到 E 盘", self.scanner))


class PayloadTests(unittest.TestCase):
    def test_payload_fields_and_provenance(self):
        payload = _payload(
            42, "preference", "工具放 E 盘", 0.92,
            "memory-os-unit", "test-workspace",
        )
        self.assertEqual(payload["event_id"], "mos_42")
        self.assertEqual(payload["workspace_id"], "test-workspace")
        self.assertEqual(payload["scope"], "workspace")
        self.assertEqual(payload["kind"], "preference")
        self.assertEqual(payload["evidence"], "user_explicit")
        self.assertEqual(payload["confidence"], 0.92)
        provenance = payload["metadata"]["provenance"]
        self.assertEqual(provenance["provider_type"], "memory-os")
        self.assertEqual(provenance["provider_instance_id"], "memory-os-unit")
        self.assertEqual(provenance["source_record_id"], "42")
        self.assertEqual(provenance["capture_mode"], "automatic_whitelist")
        self.assertRegex(provenance["source_revision"], r"^[0-9a-f]{64}$")


class ShareSyncFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "memory.db"
        _make_db(self.db_path)
        _seed(
            self.db_path,
            [
                ("preference", "工具放 E 盘", 0.9, "active"),
                ("decision", "共享 Gateway 冻结扩展", 0.88, "active"),
            ],
        )
        _mark_shared(self.db_path, [1, 2])

    def tearDown(self):
        _cleanup_tmp(self.tmp)

    def test_dry_run_does_not_call_proxy(self):
        proxy = FakeSidecar()
        result = share_sync(self.db_path, proxy=proxy, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(proxy.remembered, [])

    def test_push_records_share_log_and_is_idempotent(self):
        proxy = FakeSidecar()
        first = share_sync(self.db_path, proxy=proxy, limit=20)
        self.assertEqual(first["pushed"], 2)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(len(proxy.remembered), 2)

        second = share_sync(self.db_path, proxy=proxy, limit=20)
        self.assertEqual(second["candidate_count"], 0)
        self.assertEqual(second["pushed"], 0)
        self.assertEqual(len(proxy.remembered), 2)

    def test_sensitive_candidate_is_skipped(self):
        _seed_full(self.db_path, [("fact", "password=not-for-memory", 0.9, "active", "shared")])
        proxy = FakeSidecar()
        result = share_sync(self.db_path, proxy=proxy, limit=20)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(any("password" in str(p["content"]) for p in proxy.remembered))

    def test_private_and_sensitive_never_leave(self):
        # private 与 sensitive 记忆即使高置信也不进候选，更不会被推送
        _seed_full(
            self.db_path,
            [
                ("fact", "私密内容不应共享", 0.95, "active", "private"),
                ("fact", "sensitive 内容也不推", 0.95, "active", "sensitive"),
            ],
        )
        proxy = FakeSidecar()
        result = share_sync(self.db_path, proxy=proxy, limit=20)
        # 只有 setUp 里显式 shared 的 2 条被推，private/sensitive 不出现
        self.assertEqual(result["candidate_count"], 2)
        pushed_contents = [str(p["content"]) for p in proxy.remembered]
        self.assertFalse(any("私密" in c or "sensitive" in c for c in pushed_contents))

    def test_unavailable_sidecar_degrades(self):
        result = share_sync(self.db_path, sidecar_env_path=None, limit=20)
        self.assertTrue(result["success"])
        self.assertFalse(result["shared_available"])
        self.assertEqual(result["skipped"], 2)

    def test_reconcile_writes_back_final_receipts(self):
        proxy = FakeSidecar(status="pending")
        first = share_sync(self.db_path, proxy=proxy, limit=20)
        self.assertEqual(first["pushed"], 2)
        self.assertEqual(first["reconciled"]["confirmed"], 0)

        # 中枢处理完成后，下一轮对账应把 pending 回写为 applied
        proxy.receipts = {"mos_1": "applied", "mos_2": "duplicate"}
        second = share_sync(self.db_path, proxy=proxy, limit=20)
        self.assertEqual(second["pushed"], 0)
        self.assertEqual(second["reconciled"]["confirmed"], 2)
        self.assertEqual(second["reconciled"]["pending_in_flight"], 0)

        connection = sqlite3.connect(self.db_path)
        statuses = dict(
            connection.execute("SELECT memory_id, status FROM share_log ORDER BY memory_id")
        )
        connection.close()
        self.assertEqual(statuses[1], "applied")
        self.assertEqual(statuses[2], "duplicate")

    def test_reconcile_keeps_pending_without_receipt(self):
        proxy = FakeSidecar(status="pending")
        first = share_sync(self.db_path, proxy=proxy, limit=20)
        self.assertEqual(first["pushed"], 2)

        # 无回执时保持 pending，等下一轮
        second = share_sync(self.db_path, proxy=proxy, limit=20)
        self.assertEqual(second["reconciled"]["confirmed"], 0)
        self.assertEqual(second["reconciled"]["pending_in_flight"], 2)

        connection = sqlite3.connect(self.db_path)
        statuses = dict(
            connection.execute("SELECT memory_id, status FROM share_log ORDER BY memory_id")
        )
        connection.close()
        self.assertEqual(statuses[1], "pending")
        self.assertEqual(statuses[2], "pending")


if __name__ == "__main__":
    unittest.main()
