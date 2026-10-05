import os
import unittest
from contextlib import nullcontext

from agent_memory_gateway.auth import Principal
from agent_memory_gateway.crystal_service import (
    PostgresCrystalCandidatePlanner,
    PostgresCrystalService,
    mark_crystal_stale,
    scope_binding_hash,
)


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
        if normalized.startswith("UPDATE crystal_rebuild_candidates"):
            return Cursor()
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
        self.assertTrue(
            any("UPDATE crystal_rebuild_candidates" in sql for sql, _ in connection.executed)
        )

    def test_source_change_only_marks_existing_page_stale(self):
        connection = Connection()
        mark_crystal_stale(connection, "a" * 64, 9)
        statement, params = connection.executed[-1]
        self.assertIn("UPDATE memory_crystals", statement)
        self.assertEqual(params, (9, "a" * 64))

    def test_candidate_planner_stores_only_references_and_is_idempotent(self):
        class PlannerCursor(Cursor):
            def __init__(self, *, rows=None, rowcount=0):
                super().__init__(rows=rows)
                self.rowcount = rowcount

        class PlannerConnection(Connection):
            def execute(self, sql, params=None):
                normalized = " ".join(sql.split())
                self.executed.append((normalized, params))
                if normalized.startswith("SELECT lifecycle.scope_binding_hash"):
                    return PlannerCursor(
                        rows=[
                            (
                                "a" * 64,
                                "personal",
                                "lee",
                                "workspace-a",
                                "workspace",
                                "device:pc",
                                ["gbrain:fact:11", "gbrain:fact:12"],
                                2,
                                31,
                                "stale",
                                30,
                            )
                        ]
                    )
                if normalized.startswith("INSERT INTO crystal_rebuild_candidates"):
                    return PlannerCursor(rowcount=1)
                return PlannerCursor()

        connection = PlannerConnection()
        planner = PostgresCrystalCandidatePlanner(
            "postgresql://test", connection_factory=lambda: connection
        )
        self.assertEqual(planner.plan(limit=10), 1)
        insert = next(
            (params for sql, params in connection.executed if "INSERT INTO crystal_rebuild_candidates" in sql),
            None,
        )
        self.assertIsNotNone(insert)
        self.assertIn("gbrain:fact:11", repr(insert))
        self.assertNotIn("记忆正文", repr(insert))

    def test_list_candidates_returns_references_without_content(self):
        class CandidateConnection(Connection):
            def execute(self, sql, params=None):
                normalized = " ".join(sql.split())
                self.executed.append((normalized, params))
                if "JOIN workspace_bindings" in normalized:
                    return Cursor((1,))
                if "FROM crystal_rebuild_candidates" in normalized:
                    return Cursor(
                        rows=[
                            (
                                "candidate-1",
                                "a" * 64,
                                "workspace",
                                "device:pc",
                                ["gbrain:fact:11", "gbrain:fact:12"],
                                2,
                                31,
                                "stale",
                                "2026-08-13T00:00:00Z",
                                "2026-08-13T01:00:00Z",
                            )
                        ]
                    )
                return Cursor()

        service = PostgresCrystalService(
            "postgresql://test", GBrain(), connection_factory=lambda: CandidateConnection()
        )
        result = service.list_candidates(
            {"workspace_id": "workspace-a", "limit": 10}, principal()
        )
        self.assertEqual(result["candidates"][0]["reason"], "stale")
        self.assertNotIn("content", result["candidates"][0])


@unittest.skipUnless(os.environ.get("MEMORY_TEST_POSTGRES_DSN"), "需要 PostgreSQL 验证连接")
class CrystalPlannerPostgresTests(unittest.TestCase):
    """使用迁移后的真实表结构；所有写入仅发生在当前连接的临时表中。"""

    def setUp(self):
        import psycopg
        from psycopg import sql

        self.connection = psycopg.connect(os.environ["MEMORY_TEST_POSTGRES_DSN"])
        self.addCleanup(self.connection.close)
        for table in ("memory_lifecycle", "memory_crystals", "crystal_rebuild_candidates"):
            self.connection.execute(
                sql.SQL("CREATE TEMP TABLE {} (LIKE public.{} INCLUDING ALL)").format(
                    sql.Identifier(table), sql.Identifier(table)
                )
            )
        self.planner = PostgresCrystalCandidatePlanner(
            "postgresql://test", connection_factory=lambda: nullcontext(self.connection)
        )

    def add_sources(self, label, *, count=2, revision=10, status="active",
                    instruction_like=False, binding=True, tenant="personal", user="lee",
                    workspace="workspace-a", scope="workspace", namespace=None):
        namespace = namespace or label
        binding_hash = scope_binding_hash(tenant, user, workspace, scope, namespace)
        for index in range(count):
            reference = f"gbrain:fact:{label}-{index}"
            self.connection.execute(
                """
                INSERT INTO memory_lifecycle (
                  backend_ref, tenant_id, user_id, workspace_id, scope, namespace_key,
                  source_device_id, source_agent_installation_id, source_event_id,
                  evidence, confidence, instruction_like, status, scope_binding_hash,
                  created_server_revision, updated_server_revision
                ) VALUES (%s, %s, %s, %s, %s, %s, 'test-device', 'test-agent', %s,
                          'user_explicit', 1, %s, %s, %s, 1, %s)
                """,
                (reference, tenant, user, workspace, scope, namespace, reference,
                 instruction_like, status, binding_hash if binding else None, revision),
            )
        return binding_hash

    def add_crystal(self, label, *, status="ready", revision=10):
        self.connection.execute(
            """
            INSERT INTO memory_crystals (
              scope_binding_hash, tenant_id, user_id, workspace_id, scope,
              namespace_key, status, generated_server_revision
            ) VALUES (%s, 'personal', 'lee', 'workspace-a', 'workspace', %s, %s, %s)
            """,
            (scope_binding_hash("personal", "lee", "workspace-a", "workspace", label),
             label, status, revision),
        )

    def candidates(self):
        return self.connection.execute(
            """
            SELECT namespace_key, reason, source_refs, source_count, source_revision, status
            FROM crystal_rebuild_candidates ORDER BY namespace_key
            """
        ).fetchall()

    def test_only_eligible_sources_plan_missing_stale_and_changed_crystals(self):
        for label in ("missing", "stale", "changed", "ready", "failed"):
            self.add_sources(label)
        for label, status, revision in (
            ("stale", "stale", 10), ("changed", "ready", 9),
            ("ready", "ready", 10), ("failed", "failed", 10),
        ):
            self.add_crystal(label, status=status, revision=revision)
        for label, options in (
            ("single", {"count": 1}), ("archived", {"status": "archived"}),
            ("withdrawn", {"status": "pending_deletion"}),
            ("superseded", {"status": "superseded"}),
            ("instruction", {"instruction_like": True}), ("unbound", {"binding": False}),
        ):
            self.add_sources(label, **options)
        self.add_sources("missing-archived", status="archived", namespace="missing")
        self.add_sources("missing-instruction", instruction_like=True, namespace="missing")

        self.assertEqual(self.planner.plan(), 3)
        self.assertEqual(self.candidates(), [
            ("changed", "source_changed", ["gbrain:fact:changed-0", "gbrain:fact:changed-1"], 2, 10, "pending"),
            ("missing", "missing", ["gbrain:fact:missing-0", "gbrain:fact:missing-1"], 2, 10, "pending"),
            ("stale", "stale", ["gbrain:fact:stale-0", "gbrain:fact:stale-1"], 2, 10, "pending"),
        ])

    def test_scopes_do_not_combine_sources_across_users_or_workspaces(self):
        for label, options in (
            ("base", {}), ("tenant", {"tenant": "other"}), ("user", {"user": "other"}),
            ("workspace", {"workspace": "other"}), ("scope", {"scope": "private"}),
            ("namespace", {"namespace": "other"}),
        ):
            self.add_sources(label, count=1, **({"namespace": "same"} | options))
        self.assertEqual(self.planner.plan(), 0)
        self.add_sources("base-second", count=1, namespace="same")
        self.assertEqual(self.planner.plan(), 1)
        self.assertEqual(self.candidates()[0][2], ["gbrain:fact:base-0", "gbrain:fact:base-second-0"])

    def test_repeated_plans_preserve_dismissal_until_sources_change(self):
        self.add_sources("missing")
        self.assertEqual(self.planner.plan(), 1)
        self.assertEqual(self.planner.plan(), 0)
        self.connection.execute("UPDATE crystal_rebuild_candidates SET status = 'dismissed'")
        self.assertEqual(self.planner.plan(), 0)
        self.assertEqual(self.candidates()[0][-1], "dismissed")
        self.connection.execute("UPDATE memory_lifecycle SET updated_server_revision = 11")
        self.assertEqual(self.planner.plan(), 1)
        self.assertEqual(self.candidates()[0][-2:], (11, "pending"))
        self.assertEqual(self.planner.plan(), 0)

    def test_limit_orders_oldest_source_revision_first(self):
        self.add_sources("newer", revision=20)
        self.add_sources("older", revision=10)
        self.assertEqual(self.planner.plan(limit=1), 1)
        self.assertEqual(self.candidates()[0][0], "older")
