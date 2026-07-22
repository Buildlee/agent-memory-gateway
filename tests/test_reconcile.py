import sys
import unittest
import base64
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.crypto import EventCipher
from agent_memory_gateway.reconcile import PendingEventWorker, ReconcileResult, reconcile_cycle


class ReconcileResultTests(unittest.TestCase):
    def test_result_omits_unavailable_fields(self):
        self.assertEqual(ReconcileResult(status="idle").as_dict(), {"status": "idle"})

    def test_applied_result_contains_stable_receipt_references(self):
        result = ReconcileResult("applied", "evt_1", "gbrain:fact:42", 7).as_dict()
        self.assertEqual(result["event_id"], "evt_1")
        self.assertEqual(result["backend_ref"], "gbrain:fact:42")
        self.assertEqual(result["server_revision"], 7)

    def test_external_provenance_accepts_only_opaque_safe_identifiers(self):
        payload = {
            "metadata": {
                "provenance": {
                    "provider_type": "files",
                    "provider_instance_id": "hermes-local",
                    "source_record_id": "local_123",
                    "source_revision": "a" * 64,
                    "capture_mode": "manual_selection",
                }
            }
        }
        provenance = PendingEventWorker._external_provenance(payload)
        self.assertEqual(provenance["provider_instance_id"], "hermes-local")
        payload["metadata"]["provenance"]["source_record_id"] = "C:\\private\\MEMORY.md"
        self.assertIsNone(PendingEventWorker._external_provenance(payload))

    def test_external_binding_contains_no_content_or_file_path(self):
        class Connection:
            def __init__(self):
                self.sql = ""
                self.params = None

            def execute(self, sql, params=None):
                self.sql = " ".join(sql.split())
                self.params = params
                return Cursor()

        connection = Connection()
        principal = type("P", (), {
            "tenant_id": "personal",
            "user_id": "lee",
            "device_id": "pc",
            "agent_installation_id": "codex",
            "workspace_ids": frozenset({"workspace-a"}),
        })()
        provenance = {
            "provider_type": "files",
            "provider_instance_id": "codex-local",
            "source_record_id": "local_123",
            "source_revision": "b" * 64,
            "capture_mode": "automatic_whitelist",
        }
        PendingEventWorker._register_external_binding(
            connection, principal, "evt_1", provenance, None
        )
        self.assertIn("workspace_id, provider_instance_id", connection.sql)
        serialized = repr(connection.params)
        self.assertIn("local_123", serialized)
        self.assertNotIn("MEMORY.md", serialized)
        self.assertNotIn("content", connection.sql.lower())


class ReconcileCycleTests(unittest.TestCase):
    def test_once_calls_only_one_event(self):
        class Worker:
            def reconcile_once(self):
                return ReconcileResult("idle")

            def reconcile(self, _limit):
                raise AssertionError("单条周期不应调用批量方法")

        self.assertEqual(reconcile_cycle(Worker(), once=True, limit=100), [ReconcileResult("idle")])

    def test_batch_calls_limited_reconcile(self):
        class Worker:
            def reconcile_once(self):
                raise AssertionError("批量周期不应调用单条方法")

            def reconcile(self, limit):
                self.limit = limit
                return [ReconcileResult("idle")]

        worker = Worker()
        self.assertEqual(reconcile_cycle(worker, once=False, limit=17), [ReconcileResult("idle")])
        self.assertEqual(worker.limit, 17)


class WorkerHeartbeatTests(unittest.TestCase):
    def test_heartbeat_uses_gateway_state_without_event_content(self):
        class Connection:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def transaction(self):
                return self

            def execute(self, sql, params=None):
                self.calls.append((" ".join(sql.split()), params))
                return Cursor()

        connection = Connection()
        cipher = EventCipher.from_base64(base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="))
        worker = PendingEventWorker("postgresql://test", cipher, object(), connection_factory=lambda: connection)
        worker.record_heartbeat()

        self.assertEqual(len(connection.calls), 1)
        sql, params = connection.calls[0]
        self.assertIn("worker_heartbeat", sql)
        self.assertNotIn("gateway_events", sql)
        self.assertEqual(len(params), 1)


class Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class RecoveringConnection:
    def __init__(self, event_row):
        self.event_row = event_row
        self.pending = True
        self.receipts = 0
        self.bindings = 0
        self.revision = 11

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return self

    def execute(self, sql, _params=None):
        normalized = " ".join(sql.split())
        if "FROM gateway_events" in normalized and "FOR UPDATE SKIP LOCKED" in normalized:
            return Cursor(self.event_row if self.pending else None)
        if "SELECT state_value FROM gateway_state" in normalized:
            return Cursor((str(self.revision),))
        if normalized.startswith("UPDATE gateway_state"):
            self.revision += 1
            return Cursor()
        if normalized.startswith("UPDATE memory_crystals"):
            return Cursor()
        if normalized.startswith("UPDATE gateway_events"):
            self.pending = False
            return Cursor()
        if "INSERT INTO backend_bindings" in normalized:
            self.bindings += 1
            return Cursor()
        if "INSERT INTO event_receipts" in normalized:
            self.receipts += 1
            return Cursor()
        if (
            "INSERT INTO gateway_state" in normalized
            or "INSERT INTO audit_log" in normalized
            or "INSERT INTO memory_lifecycle" in normalized
            or "INSERT INTO memory_lifecycle_history" in normalized
        ):
            return Cursor()
        raise AssertionError(normalized)


class ExistingEffectBackend:
    def __init__(self):
        self.domain_effects = 1
        self.calls = 0

    def upsert_confirmed(self, **_kwargs):
        self.calls += 1
        return "gbrain:fact:42"


class WorkerCrashRecoveryTests(unittest.TestCase):
    def test_restart_recovers_existing_backend_effect_without_duplicate(self):
        encoded = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
        cipher = EventCipher.from_base64(encoded)
        envelope = {
            "event_id": "evt_crash_recovery",
            "payload": {
                "content": "GBrain 已写但 Gateway 尚未记录时必须恢复。",
                "kind": "decision",
                "evidence": "user_explicit",
                "confidence": 1.0,
            },
        }
        aad = b"personal:lee:pc:codex:evt_crash_recovery"
        encrypted = cipher.encrypt_json(envelope, aad=aad)
        row = (
            "pc",
            "evt_crash_recovery",
            "personal",
            "lee",
            "codex",
            "workspace-a",
            encrypted.ciphertext,
            encrypted.nonce,
            encrypted.key_version,
            False,
            0,
        )
        connection = RecoveringConnection(row)
        backend = ExistingEffectBackend()
        worker = PendingEventWorker(
            "postgresql://test",
            cipher,
            backend,
            connection_factory=lambda: connection,
        )

        recovered = worker.reconcile_once()
        idle = worker.reconcile_once()

        self.assertEqual(recovered.status, "applied")
        self.assertEqual(recovered.backend_ref, "gbrain:fact:42")
        self.assertEqual(recovered.server_revision, 12)
        self.assertEqual(idle.status, "idle")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(backend.domain_effects, 1)
        self.assertEqual(connection.bindings, 1)
        self.assertEqual(connection.receipts, 1)
