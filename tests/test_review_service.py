import base64
import unittest

from agent_memory_gateway.auth import Principal
from agent_memory_gateway.crypto import EventCipher
from agent_memory_gateway.review_service import PostgresReviewService, ReviewError


def reviewer() -> Principal:
    return Principal(
        tenant_id="personal",
        user_id="lee",
        device_id="review-pc",
        agent_installation_id="codex-review",
        workspace_ids=frozenset({"workspace-a"}),
        capabilities=frozenset({"memory.manage", "memory.forget"}),
        workspace_capabilities={"workspace-a": frozenset({"memory.manage", "memory.forget"})},
    )


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = list(rows or [])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class UpdateCursor(Cursor):
    def __init__(self, rowcount):
        super().__init__()
        self.rowcount = rowcount


class TombstoneConnection:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return UpdateCursor(self.rowcount)


class ReviewConnection:
    def __init__(self, candidate_row, *, conflicts=None, original_operation=None):
        self.candidate_row = candidate_row
        self.conflicts = list(conflicts or [])
        self.original_operation = original_operation
        self.executed = []
        self.revision = 20

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "JOIN workspace_bindings" in normalized:
            return Cursor((1,))
        if "FROM review_operations" in normalized and "idempotency_key" in normalized:
            return Cursor(None)
        if "FROM review_candidates AS c" in normalized and "WHERE c.status = 'pending'" in normalized:
            return Cursor(rows=[self.candidate_row])
        if "FROM review_candidates AS c" in normalized:
            return Cursor(self.candidate_row)
        if normalized.startswith("SELECT operation_id, action, backend_ref, target_ref"):
            return Cursor(self.original_operation)
        if "compensates_operation_id" in normalized and normalized.startswith("SELECT 1"):
            return Cursor(None)
        if normalized.startswith("SELECT backend_ref, evidence, confidence"):
            return Cursor(rows=self.conflicts)
        if normalized.startswith("SELECT status, pinned FROM memory_lifecycle"):
            return Cursor(("active", False))
        if normalized.startswith("SELECT status, updated_server_revision FROM memory_lifecycle"):
            return Cursor(("active", 20))
        if "SELECT state_value FROM gateway_state" in normalized:
            return Cursor((str(self.revision),))
        if normalized.startswith("UPDATE gateway_state"):
            self.revision += 1
            return Cursor()
        if normalized.startswith("UPDATE memory_tombstones SET revoked_revision"):
            return UpdateCursor(1)
        return Cursor()


class FakeGBrain:
    def __init__(self):
        self.upserts = []
        self.supersedes = []
        self.archives = []
        self.restores = []

    def upsert_confirmed(self, **kwargs):
        self.upserts.append(kwargs)
        return "gbrain:fact:200"

    def supersede(self, **kwargs):
        self.supersedes.append(kwargs)
        return kwargs["new_ref"]

    def archive(self, **kwargs):
        self.archives.append(kwargs)
        return kwargs["reference"]

    def restore_superseded(self, **kwargs):
        self.restores.append(kwargs)
        return kwargs["old_ref"]


class ReviewServiceTests(unittest.TestCase):
    def setUp(self):
        encoded = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
        self.cipher = EventCipher.from_base64(encoded)

    def candidate_row(self, *, status="pending", revision=1, last_operation_id=None):
        event_id = "evt_review_1"
        origin = Principal(
            tenant_id="personal",
            user_id="lee",
            device_id="source-pc",
            agent_installation_id="codex-source",
            workspace_ids=frozenset({"workspace-a"}),
            capabilities=frozenset(),
        )
        encrypted = self.cipher.encrypt_json(
            {
                "event_id": event_id,
                "payload": {
                    "content": "办公室网关端口是 8787。",
                    "kind": "fact",
                    "requested_scope": "workspace",
                    "evidence": "agent_observed",
                    "confidence": 0.8,
                    "metadata": {"entity_key": "gateway", "attribute_key": "port"},
                    "instruction_like": False,
                },
            },
            aad=b"personal:lee:source-pc:codex-source:evt_review_1",
        )
        return (
            "review_1", revision, status, None, None,
            "source-pc", event_id, "personal", "lee", "codex-source", "workspace-a", "workspace",
            encrypted.ciphertext, encrypted.nonce, encrypted.key_version, last_operation_id,
        )

    def service(self, connection, backend):
        return PostgresReviewService(
            "postgresql://test",
            self.cipher,
            backend,
            connection_factory=lambda: connection,
        )

    def test_confirm_creates_user_confirmed_fact_and_operation(self):
        connection = ReviewConnection(self.candidate_row())
        backend = FakeGBrain()
        result = self.service(connection, backend).resolve(
            {
                "workspace_id": "workspace-a",
                "review_id": "review_1",
                "expected_revision": 1,
                "action": "confirm",
                "idempotency_key": "review-confirm-1",
            },
            reviewer(),
        )
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["backend_ref"], "gbrain:fact:200")
        self.assertEqual(result["review_revision"], 2)
        self.assertEqual(len(backend.upserts), 1)
        self.assertTrue(any("INSERT INTO review_operations" in sql for sql, _ in connection.executed))
        self.assertTrue(any("INSERT INTO memory_lifecycle" in sql for sql, _ in connection.executed))
        self.assertTrue(any("UPDATE external_memory_bindings" in sql for sql, _ in connection.executed))
        operation_index = next(
            index for index, (sql, _) in enumerate(connection.executed)
            if "INSERT INTO review_operations" in sql
        )
        lifecycle_history_index = next(
            index for index, (sql, _) in enumerate(connection.executed)
            if "INSERT INTO memory_lifecycle_history" in sql
        )
        self.assertLess(operation_index, lifecycle_history_index)

    def test_same_semantic_key_requires_explicit_conflict_choice(self):
        connection = ReviewConnection(
            self.candidate_row(),
            conflicts=[("gbrain:fact:199", "user_confirmed", 1.0, False, "workspace", "source-pc", "codex")],
        )
        backend = FakeGBrain()
        result = self.service(connection, backend).resolve(
            {
                "workspace_id": "workspace-a",
                "review_id": "review_1",
                "expected_revision": 1,
                "action": "confirm",
                "idempotency_key": "review-conflict-1",
            },
            reviewer(),
        )
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["suggested_action"], "retain_both")
        self.assertEqual(backend.upserts, [])

    def test_list_pending_uses_null_safe_temporal_key_comparison(self):
        connection = ReviewConnection(self.candidate_row())
        result = self.service(connection, FakeGBrain()).list_pending(
            {"workspace_id": "workspace-a", "limit": 10},
            reviewer(),
        )
        self.assertEqual(result["count"], 1)
        conflict_query, parameters = next(
            (sql, params)
            for sql, params in connection.executed
            if "FROM memory_lifecycle" in sql
        )
        self.assertIn("temporal_key IS NOT DISTINCT FROM %s", conflict_query)
        self.assertEqual(parameters[-1], None)

    def test_supersede_and_revert_are_compensating_operations(self):
        conflicts = [("gbrain:fact:199", "agent_observed", 0.6, False, "workspace", "source-pc", "codex")]
        connection = ReviewConnection(self.candidate_row(), conflicts=conflicts)
        backend = FakeGBrain()
        service = self.service(connection, backend)
        confirmed = service.resolve(
            {
                "workspace_id": "workspace-a",
                "review_id": "review_1",
                "expected_revision": 1,
                "action": "supersede",
                "target_ref": "gbrain:fact:199",
                "idempotency_key": "review-supersede-1",
            },
            reviewer(),
        )
        self.assertEqual(confirmed["superseded_ref"], "gbrain:fact:199")
        self.assertEqual(len(backend.supersedes), 1)

        revert_connection = ReviewConnection(
            self.candidate_row(status="confirmed", revision=2, last_operation_id="rvop_original"),
            original_operation=("rvop_original", "supersede", "gbrain:fact:200", "gbrain:fact:199"),
        )
        reverted = self.service(revert_connection, backend).revert(
            {
                "workspace_id": "workspace-a",
                "review_id": "review_1",
                "operation_id": "rvop_original",
                "expected_revision": 2,
                "idempotency_key": "review-revert-1",
            },
            reviewer(),
        )
        self.assertEqual(reverted["status"], "reverted")
        self.assertEqual(len(backend.restores), 1)
        self.assertTrue(any("compensates_operation_id" in sql for sql, _ in revert_connection.executed))
        revert_operation_index = next(
            index for index, (sql, _) in enumerate(revert_connection.executed)
            if "INSERT INTO review_operations" in sql
        )
        revert_history_index = next(
            index for index, (sql, _) in enumerate(revert_connection.executed)
            if "INSERT INTO memory_lifecycle_history" in sql
        )
        self.assertLess(revert_operation_index, revert_history_index)
        tombstone_revoke_query, tombstone_revoke_params = next(
            (sql, params)
            for sql, params in revert_connection.executed
            if sql.startswith("UPDATE memory_tombstones SET revoked_revision")
        )
        self.assertNotIn("DELETE", tombstone_revoke_query)
        self.assertEqual(tombstone_revoke_params, (21, "gbrain:fact:199", "personal", "lee"))

    def test_forget_archives_memory_and_writes_tombstone(self):
        connection = ReviewConnection(self.candidate_row())
        backend = FakeGBrain()
        result = self.service(connection, backend).forget(
            {"workspace_id": "workspace-a", "memory_id": "gbrain:fact:200"}, reviewer()
        )
        self.assertEqual(result["status"], "archived")
        self.assertEqual(backend.archives[0]["reference"], "gbrain:fact:200")
        self.assertTrue(any("UPDATE memory_lifecycle SET status = 'archived'" in sql for sql, _ in connection.executed))
        self.assertTrue(any("INSERT INTO memory_tombstones" in sql for sql, _ in connection.executed))

    def test_revoke_tombstone_requires_an_active_record(self):
        connection = TombstoneConnection(rowcount=0)
        with self.assertRaisesRegex(ReviewError, "SUPERSEDE_TOMBSTONE_MISSING"):
            PostgresReviewService._revoke_tombstone(connection, "gbrain:fact:199", reviewer(), 21)
