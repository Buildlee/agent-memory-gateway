import base64
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_memory_gateway.crypto import EventCipher
from agent_memory_gateway.outbox import LegacyOutboxError, Outbox, OutboxInUseError


def cipher() -> EventCipher:
    return EventCipher.from_base64(base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="))


class OutboxEncryptionTests(unittest.TestCase):
    def test_process_lock_allows_only_one_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.db"
            first = Outbox(path, cipher())
            try:
                with self.assertRaises(OutboxInUseError):
                    Outbox(path, cipher())
            finally:
                first.close()
            reopened = Outbox(path, cipher())
            reopened.close()

    def test_concurrent_enqueue_serializes_100_device_sequences(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(Path(directory) / "outbox.db", cipher())
            try:
                def enqueue(index: int) -> int:
                    event = outbox.prepare_event({"content": f"并发事实 {index}"})
                    outbox.enqueue(event)
                    return int(event["device_seq"])

                with ThreadPoolExecutor(max_workers=20) as executor:
                    sequences = list(executor.map(enqueue, range(100)))
                self.assertEqual(sorted(sequences), list(range(1, 101)))
                self.assertEqual(outbox.count(), 100)
                self.assertEqual(
                    [int(event["device_seq"]) for event in outbox.list_events()],
                    list(range(1, 101)),
                )
            finally:
                outbox.close()

    def test_event_is_encrypted_and_sequence_is_monotonic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.db"
            outbox = Outbox(path, cipher())
            try:
                first = outbox.prepare_event({"content": "绝不能以明文存储"})
                second = outbox.prepare_event({"content": "第二条"})
                self.assertEqual((first["device_seq"], second["device_seq"]), (1, 2))
                outbox.enqueue(first)
                raw = outbox.conn.execute("SELECT payload_ciphertext FROM outbox_events_v3").fetchone()[0]
                self.assertNotIn("绝不能以明文存储".encode("utf-8"), bytes(raw))
                self.assertEqual(outbox.list_events()[0]["content"], "绝不能以明文存储")
            finally:
                outbox.close()

    def test_legacy_plaintext_events_require_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE outbox_events (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)")
            connection.execute("INSERT INTO outbox_events VALUES ('legacy', '{\"content\":\"old\"}')")
            connection.commit()
            connection.close()
            with self.assertRaises(LegacyOutboxError):
                Outbox(path, cipher())

    def test_v2_ciphertext_is_copied_once_without_deleting_old_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.db"
            event = {
                "event_id": "evt-v2",
                "device_seq": 1,
                "occurred_at": "2026-07-12T12:00:00Z",
                "content": "旧版密文只迁移一次",
            }
            encrypted = cipher().encrypt_json(event, aad=b"memory-sidecar-outbox:evt-v2")
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE outbox_state (
                  state_key TEXT PRIMARY KEY, state_value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE outbox_events_v2 (
                  id TEXT PRIMARY KEY,
                  device_seq INTEGER NOT NULL UNIQUE,
                  state TEXT NOT NULL,
                  payload_ciphertext BLOB NOT NULL,
                  payload_nonce BLOB NOT NULL,
                  payload_key_version TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO outbox_events_v2 VALUES (?, 1, 'pending', ?, ?, ?, ?, ?)",
                (
                    "evt-v2",
                    encrypted.ciphertext,
                    encrypted.nonce,
                    encrypted.key_version,
                    "2026-07-12T12:00:00Z",
                    "2026-07-12T12:00:00Z",
                ),
            )
            connection.commit()
            connection.close()

            outbox = Outbox(path, cipher())
            try:
                self.assertEqual(outbox.list_events()[0]["content"], "旧版密文只迁移一次")
                self.assertEqual(
                    outbox.conn.execute("SELECT count(*) FROM outbox_events_v2").fetchone()[0],
                    1,
                )
                outbox.mark_terminal(
                    "evt-v2",
                    {"status": "applied", "ack_id": "ack-1", "server_revision": 9},
                )
                self.assertEqual(outbox.cleanup_acked(), 1)
            finally:
                outbox.close()

            reopened = Outbox(path, cipher())
            try:
                self.assertEqual(reopened.count(), 0)
                self.assertEqual(
                    reopened.conn.execute("SELECT count(*) FROM outbox_events_v2").fetchone()[0],
                    1,
                )
            finally:
                reopened.close()

    def test_in_flight_event_recovers_after_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.db"
            outbox = Outbox(path, cipher())
            event = outbox.prepare_event({"content": "崩溃后重试"})
            outbox.enqueue(event)
            outbox.mark_in_flight([str(event["event_id"])])
            outbox.close()

            recovered = Outbox(path, cipher())
            try:
                self.assertEqual(recovered.status_counts(), {"retryable_failed": 1})
                self.assertEqual(recovered.list_events()[0]["content"], "崩溃后重试")
            finally:
                recovered.close()

    def test_pull_page_updates_encrypted_cache_and_cursor_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(Path(directory) / "outbox.db", cipher())
            try:
                outbox.apply_pull_page(
                    "workspace-a",
                    {
                        "sync_epoch": "sync-1",
                        "policy_version": "policy-1",
                        "auth_epoch": {"device": 2, "agent": 3},
                        "memories": [
                            {
                                "memory_id": "gbrain:fact:42",
                                "server_revision": 5,
                                "content": "缓存正文必须保持密文",
                                "content_role": "reference_data",
                                "instruction_like": False,
                                "status": "confirmed",
                            },
                            {
                                "memory_id": "gbrain:fact:43",
                                "server_revision": 6,
                                "content": "仍然有效的缓存",
                                "content_role": "reference_data",
                                "instruction_like": False,
                                "status": "confirmed",
                            },
                        ],
                        "tombstones": [
                            {"backend_ref": "gbrain:fact:42", "deleted_revision": 7}
                        ],
                        "next_revision": 7,
                        "next_cursor": "cursor-2",
                    },
                )
                self.assertEqual(
                    [item["memory_id"] for item in outbox.cache_search("workspace-a", "缓存")],
                    ["gbrain:fact:43"],
                )
                raw = outbox.conn.execute(
                    "SELECT payload_ciphertext FROM sidecar_cache_v1 WHERE cache_id = 'gbrain:fact:43'"
                ).fetchone()[0]
                self.assertNotIn("仍然有效的缓存".encode("utf-8"), bytes(raw))
                state = outbox.sync_state("workspace-a")
                self.assertEqual(state["last_seen_revision"], 7)
                self.assertEqual(state["cursor"], "cursor-2")
                self.assertEqual(state["device_auth_epoch"], 2)
                self.assertEqual(state["agent_auth_epoch"], 3)
            finally:
                outbox.close()

    def test_epoch_reset_clears_only_target_workspace_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Outbox(Path(directory) / "outbox.db", cipher())
            try:
                for workspace, memory_id in (("workspace-a", "fact-a"), ("workspace-b", "fact-b")):
                    outbox.apply_pull_page(
                        workspace,
                        {
                            "sync_epoch": "sync-old",
                            "policy_version": "policy-1",
                            "auth_epoch": {"device": 1, "agent": 1},
                            "memories": [
                                {
                                    "memory_id": memory_id,
                                    "server_revision": 1,
                                    "content": workspace,
                                    "content_role": "reference_data",
                                    "instruction_like": False,
                                    "status": "confirmed",
                                }
                            ],
                            "tombstones": [],
                            "next_revision": 1,
                            "next_cursor": None,
                        },
                    )
                outbox.apply_pull_page(
                    "workspace-a",
                    {"sync_epoch": "sync-new", "reset_required": True},
                )
                self.assertEqual(outbox.cache_search("workspace-a", ""), [])
                self.assertEqual(len(outbox.cache_search("workspace-b", "")), 1)
                self.assertEqual(outbox.sync_state("workspace-a")["sync_epoch"], "sync-new")
            finally:
                outbox.close()
