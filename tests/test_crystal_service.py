import unittest

from agent_memory_gateway.auth import Principal
from agent_memory_gateway.crystal_service import PostgresCrystalService, mark_crystal_stale


def principal() -> Principal:
    return Principal(
        tenant_id="personal",
        user_id="lee",
        device_id="pc",
        agent_installation_id="codex",
        workspace_ids=frozenset({"workspace-a"}),
        capabilities=frozenset({"memory.manage"}),
    )


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = list(rows or [])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self):
        self.revision = 30
        self.executed = []

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
        if normalized.startswith("SELECT backend_ref FROM memory_lifecycle"):
            return Cursor(rows=[("gbrain:fact:11",), ("gbrain:fact:12",)])
        if "SELECT state_value FROM gateway_state" in normalized:
            return Cursor((str(self.revision),))
        if normalized.startswith("UPDATE gateway_state"):
            self.revision += 1
        return Cursor()


class GBrain:
    def __init__(self):
        self.calls = []

    def rebuild_crystal(self, **kwargs):
        self.calls.append(kwargs)
        return "gbrain:page:9"


class CrystalServiceTests(unittest.TestCase):
    def test_rebuild_uses_only_active_authorized_lifecycle_references(self):
        connection = Connection()
        backend = GBrain()
        service = PostgresCrystalService(
            "postgresql://test", backend, connection_factory=lambda: connection
        )
        result = service.rebuild(
            {
                "workspace_id": "workspace-a",
                "scope": "workspace",
                "namespace_key": "device:pc",
                "idempotency_key": "crystal-1",
            },
            principal(),
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["page_ref"], "gbrain:page:9")
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(backend.calls[0]["source_refs"], ["gbrain:fact:11", "gbrain:fact:12"])
        self.assertTrue(any("INSERT INTO memory_crystals" in sql for sql, _ in connection.executed))

    def test_source_change_only_marks_existing_page_stale(self):
        connection = Connection()
        mark_crystal_stale(connection, "a" * 64, 9)
        statement, params = connection.executed[-1]
        self.assertIn("UPDATE memory_crystals", statement)
        self.assertEqual(params, (9, "a" * 64))
