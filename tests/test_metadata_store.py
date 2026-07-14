import base64
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from agent_memory_gateway.auth import Principal
from agent_memory_gateway.crypto import EventCipher
from agent_memory_gateway.event_contract import parse_proposed_event
from agent_memory_gateway.metadata_store import MetadataStoreError, PostgresEventLedger


def principal() -> Principal:
    return Principal(
        tenant_id="personal",
        user_id="lee",
        device_id="pc",
        agent_installation_id="codex",
        workspace_ids=frozenset({"workspace-a"}),
        capabilities=frozenset({"memory.write_event"}),
    )


def payload() -> dict[str, object]:
    return {
        "event_id": "evt_fixed_receipt",
        "device_seq": 9,
        "occurred_at": "2026-07-12T04:00:00Z",
        "workspace_id": "workspace-a",
        "content": "固定回执必须在重复提交时保持不变。",
        "kind": "decision",
        "scope": "workspace",
        "evidence": "user_explicit",
    }


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class DuplicateConnection:
    def __init__(self, existing):
        self.existing = existing

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return self

    def execute(self, sql, _params=None):
        if "JOIN workspace_bindings" in sql:
            return Cursor((1,))
        if "INSERT INTO gateway_events" in sql:
            return Cursor(None)
        if "LEFT JOIN event_receipts" in sql:
            return Cursor(self.existing)
        raise AssertionError(sql)


class FixedReceiptTests(unittest.TestCase):
    def setUp(self):
        encoded = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
        self.cipher = EventCipher.from_base64(encoded)

    def test_terminal_replay_returns_original_fixed_receipt(self):
        event_hash = parse_proposed_event(payload(), principal()).payload_hash
        processed_at = datetime(2026, 7, 12, 4, 0, 2, tzinfo=timezone.utc)
        existing = (
            event_hash,
            "applied",
            "candidate_confirmed",
            None,
            "gbrain:fact:42",
            7,
            "ack_original",
            "applied",
            "candidate_confirmed",
            None,
            "gbrain:fact:42",
            7,
            "tr_original",
            processed_at,
        )
        ledger = PostgresEventLedger(
            "postgresql://test",
            self.cipher,
            connection_factory=lambda: DuplicateConnection(existing),
        )
        result = ledger.record_proposed_event(payload(), principal())
        self.assertEqual(result["status"], "duplicate")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["ack_id"], "ack_original")
        self.assertEqual(result["trace_id"], "tr_original")
        self.assertEqual(result["server_revision"], 7)
        self.assertEqual(result["processed_at"], processed_at.isoformat())

    def test_terminal_replay_fails_closed_when_receipt_is_missing(self):
        event_hash = parse_proposed_event(payload(), principal()).payload_hash
        existing = (event_hash, "applied", "candidate_confirmed", None, None, 7) + (None,) * 8
        ledger = PostgresEventLedger(
            "postgresql://test",
            self.cipher,
            connection_factory=lambda: DuplicateConnection(existing),
        )
        with self.assertRaises(MetadataStoreError):
            ledger.record_proposed_event(payload(), principal())

    def test_100_concurrent_terminal_replays_return_one_fixed_receipt(self):
        event_hash = parse_proposed_event(payload(), principal()).payload_hash
        processed_at = datetime(2026, 7, 12, 4, 0, 2, tzinfo=timezone.utc)
        existing = (
            event_hash,
            "applied",
            "candidate_confirmed",
            None,
            "gbrain:fact:42",
            7,
            "ack_original",
            "applied",
            "candidate_confirmed",
            None,
            "gbrain:fact:42",
            7,
            "tr_original",
            processed_at,
        )
        ledger = PostgresEventLedger(
            "postgresql://test",
            self.cipher,
            connection_factory=lambda: DuplicateConnection(existing),
        )
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(lambda _: ledger.record_proposed_event(payload(), principal()), range(100)))
        self.assertEqual({result["status"] for result in results}, {"duplicate"})
        self.assertEqual({result["ack_id"] for result in results}, {"ack_original"})
        self.assertEqual({result["server_revision"] for result in results}, {7})
        self.assertEqual({result["backend_ref"] for result in results}, {"gbrain:fact:42"})
