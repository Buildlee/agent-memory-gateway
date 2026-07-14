import base64
import unittest
from datetime import datetime, timezone

from agent_memory_gateway.auth import Principal
from agent_memory_gateway.crypto import EventCipher
from agent_memory_gateway.sync_service import PostgresSyncService, SyncProtocolError


def principal() -> Principal:
    return Principal(
        tenant_id="personal",
        user_id="lee",
        device_id="pc",
        agent_installation_id="codex",
        workspace_ids=frozenset({"workspace-a"}),
        capabilities=frozenset({"memory.write_event", "memory.read_context"}),
        device_auth_epoch=3,
        agent_auth_epoch=5,
    )


def cipher() -> EventCipher:
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    return EventCipher.from_base64(encoded)


class Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    def __init__(self, *, event_rows=(), tombstone_rows=(), sequences=()):
        self.event_rows = list(event_rows)
        self.tombstone_rows = list(tombstone_rows)
        self.sequences = list(sequences)
        self.checkpoint_params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "SELECT state_value FROM gateway_state" in normalized:
            return Cursor([("sync_current",)])
        if "JOIN workspace_bindings" in normalized:
            return Cursor([(1,)])
        if "INSERT INTO sync_checkpoints" in normalized:
            self.checkpoint_params = params
            return Cursor()
        if "FROM gateway_events" in normalized and "payload_ciphertext" in normalized:
            return Cursor(self.event_rows)
        if "FROM memory_tombstones" in normalized:
            return Cursor(self.tombstone_rows)
        if "SELECT last_contiguous_event_seq" in normalized:
            return Cursor([(0,)])
        if "SELECT device_seq FROM gateway_events" in normalized:
            return Cursor([(value,) for value in self.sequences])
        if normalized.startswith("UPDATE devices SET last_contiguous_event_seq"):
            return Cursor()
        raise AssertionError(normalized)


class FakeLedger:
    def __init__(self):
        self.events = []

    def record_proposed_event(self, event, _principal):
        self.events.append(event)
        return {
            "event_id": event["event_id"],
            "status": "pending",
            "retryable": True,
            "trace_id": "tr_pending",
        }


def event(event_id: str, sequence: int) -> dict[str, object]:
    return {
        "event_id": event_id,
        "device_seq": sequence,
        "occurred_at": "2026-07-12T12:00:00Z",
        "workspace_id": "workspace-a",
        "device_id": "pc",
        "agent_id": "codex",
        "content": f"同步事实 {sequence}",
    }


class PushProtocolTests(unittest.TestCase):
    def test_push_processes_each_event_and_reports_first_gap(self):
        connection = FakeConnection(sequences=(1, 3))
        ledger = FakeLedger()
        service = PostgresSyncService(
            "postgresql://test",
            ledger,
            cipher(),
            connection_factory=lambda: connection,
        )
        result = service.push(
            {
                "batch_id": "batch-1",
                "device_id": "pc",
                "workspace_id": "workspace-a",
                "protocol_version": 1,
                "sync_epoch": "sync_current",
                "events": [event("evt-1", 1), event("evt-3", 3)],
            },
            principal(),
        )
        self.assertEqual(len(ledger.events), 2)
        self.assertEqual(result["missing_device_seq"], [2])
        self.assertEqual([item["status"] for item in result["results"]], ["pending", "pending"])

    def test_push_rejects_unsorted_batch_before_writes(self):
        ledger = FakeLedger()
        service = PostgresSyncService(
            "postgresql://test",
            ledger,
            cipher(),
            connection_factory=lambda: FakeConnection(),
        )
        with self.assertRaises(SyncProtocolError) as raised:
            service.push(
                {
                    "batch_id": "batch-1",
                    "device_id": "pc",
                    "workspace_id": "workspace-a",
                    "protocol_version": 1,
                    "events": [event("evt-2", 2), event("evt-1", 1)],
                },
                principal(),
            )
        self.assertEqual(raised.exception.code, "BATCH_NOT_ORDERED")
        self.assertEqual(ledger.events, [])

    def test_stale_epoch_requests_reset_without_writing_events(self):
        ledger = FakeLedger()
        service = PostgresSyncService(
            "postgresql://test",
            ledger,
            cipher(),
            connection_factory=lambda: FakeConnection(),
        )
        result = service.push(
            {
                "batch_id": "batch-1",
                "device_id": "pc",
                "workspace_id": "workspace-a",
                "protocol_version": 1,
                "sync_epoch": "sync_stale",
                "events": [event("evt-1", 1)],
            },
            principal(),
        )
        self.assertTrue(result["reset_required"])
        self.assertEqual(result["sync_epoch"], "sync_current")
        self.assertEqual(ledger.events, [])


class PullProtocolTests(unittest.TestCase):
    def test_pull_returns_authorized_memory_and_tombstone_in_revision_order(self):
        event_cipher = cipher()
        envelope = {
            "payload": {
                "content": "只缓存已经授权的共享事实。",
                "kind": "decision",
                "confidence": 1.0,
            }
        }
        encrypted = event_cipher.encrypt_json(
            envelope,
            aad=b"personal:lee:other-pc:hermes:evt-source",
        )
        event_rows = [
            (
                "other-pc",
                "evt-source",
                "hermes",
                "workspace-a",
                "workspace",
                "gbrain:fact:42",
                5,
                encrypted.ciphertext,
                encrypted.nonce,
                encrypted.key_version,
            )
        ]
        tombstone_rows = [
            (
                "gbrain:fact:41",
                "gbrain:fact:41",
                6,
                datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
                "user_deleted",
            )
        ]
        connection = FakeConnection(event_rows=event_rows, tombstone_rows=tombstone_rows)
        service = PostgresSyncService(
            "postgresql://test",
            FakeLedger(),
            event_cipher,
            connection_factory=lambda: connection,
        )
        result = service.pull(
            {
                "workspace_id": "workspace-a",
                "protocol_version": 1,
                "sync_epoch": "sync_current",
                "last_seen_revision": 4,
                "limit": 10,
            },
            principal(),
        )
        self.assertEqual(result["memories"][0]["memory_id"], "gbrain:fact:42")
        self.assertEqual(result["memories"][0]["content_role"], "reference_data")
        self.assertEqual(result["tombstones"][0]["deleted_revision"], 6)
        self.assertEqual(result["next_revision"], 6)
        self.assertEqual(result["auth_epoch"], {"device": 3, "agent": 5})
        self.assertEqual(connection.checkpoint_params[5:8], (5, 3, 5))

    def test_cursor_is_opaque_and_bound_to_epoch_and_workspace(self):
        cursor = PostgresSyncService._encode_cursor("sync_current", "workspace-a", 12)
        self.assertEqual(
            PostgresSyncService._decode_cursor(cursor, "sync_current", "workspace-a"),
            12,
        )
        with self.assertRaises(SyncProtocolError):
            PostgresSyncService._decode_cursor(cursor, "sync_current", "workspace-b")


if __name__ == "__main__":
    unittest.main()
