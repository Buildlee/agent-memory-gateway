import unittest

from agent_memory_gateway.admin_service import AdminServiceError, PostgresAdminService
from agent_memory_gateway.auth import AuthError, Principal


def manager() -> Principal:
    return Principal(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        agent_installation_id="codex-admin",
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


class AdminConnection:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "FROM review_candidates" in normalized:
            return Cursor((2,))
        if "status IN ('pending', 'retryable_failed')" in normalized:
            return Cursor((3,))
        if "SELECT d.dead_letter_id" in normalized:
            return Cursor(
                rows=[
                    (
                        "dead-letter-7",
                        "event-7",
                        "NETWORK_TIMEOUT",
                        "GatewayTransportError",
                        "2026-07-15T12:03:00+00:00",
                        None,
                        None,
                    )
                ]
            )
        if "FROM dead_letters" in normalized:
            return Cursor((1,))
        if "COUNT(DISTINCT d.device_id)" in normalized:
            return Cursor((4,))
        if "FROM gateway_state" in normalized:
            return Cursor(("2026-07-15T12:00:00+00:00",))
        if "FROM devices AS d" in normalized:
            return Cursor(
                rows=[
                    (
                        "device-a",
                        "工作电脑",
                        "windows",
                        "active",
                        2,
                        "2026-07-15T11:59:00+00:00",
                        "codex-admin",
                        "Codex",
                        "codex",
                        "active",
                        3,
                        ["memory.search", "memory.manage"],
                        "active",
                        "2026-07-15T11:00:00+00:00",
                    )
                ]
            )
        if "FROM audit_log AS audit" in normalized:
            return Cursor(
                rows=[
                    (
                        7,
                        "device",
                        "codex-admin",
                        "review_listed",
                        "ok",
                        "trace-7",
                        "device-a",
                        "codex-admin",
                        "gbrain:fact:7",
                        "2026-07-15T12:01:00+00:00",
                        "工作电脑",
                        "windows",
                        "active",
                        "Codex",
                        "codex",
                        "active",
                    )
                ]
            )
        raise AssertionError(f"unexpected query: {normalized}")


class AdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.connection = AdminConnection()
        self.service = PostgresAdminService(
            "postgresql://test",
            connection_factory=lambda: self.connection,
        )

    def test_overview_contains_counts_but_no_memory_payload(self):
        result = self.service.overview({"workspace_id": "workspace-a"}, manager())

        self.assertEqual(result["workspace_id"], "workspace-a")
        self.assertEqual(
            result["counts"],
            {
                "pending_reviews": 2,
                "retryable_events": 3,
                "unresolved_dead_letters": 1,
                "active_devices": 4,
            },
        )
        self.assertEqual(result["worker_heartbeat_at"], "2026-07-15T12:00:00+00:00")
        self.assertNotIn("payload_ciphertext", str(result))

    def test_device_list_excludes_public_key_and_credentials(self):
        result = self.service.list_devices({"workspace_id": "workspace-a"}, manager())

        self.assertEqual(len(result["devices"]), 1)
        device = result["devices"][0]
        self.assertEqual(device["device_name"], "工作电脑")
        self.assertEqual(device["capabilities"], ["memory.manage", "memory.search"])
        self.assertNotIn("public_key", device)
        self.assertNotIn("credential_hash", device)

    def test_audit_list_excludes_details_json_and_honors_limit(self):
        result = self.service.list_audit(
            {"workspace_id": "workspace-a", "limit": 10}, manager()
        )

        self.assertEqual(result["entries"][0]["audit_id"], 7)
        self.assertEqual(result["entries"][0]["source_device_name"], "工作电脑")
        self.assertEqual(result["entries"][0]["source_agent_name"], "Codex")
        self.assertNotIn("details_json", result["entries"][0])
        audit_query = next(sql for sql, _ in self.connection.executed if "FROM audit_log AS audit" in sql)
        self.assertNotIn("details_json", audit_query)
        self.assertIn("workspace.user_id = %s", audit_query)
        self.assertIn("audit.created_at", audit_query)
        audit_params = next(params for sql, params in self.connection.executed if "FROM audit_log AS audit" in sql)
        self.assertEqual(audit_params, ("tenant-a", "user-a", "workspace-a", 10))

    def test_dead_letter_list_returns_only_repair_metadata(self):
        result = self.service.list_dead_letters(
            {"workspace_id": "workspace-a", "limit": 10}, manager()
        )

        entry = result["dead_letters"][0]
        self.assertEqual(entry["error_code"], "NETWORK_TIMEOUT")
        self.assertNotIn("payload", entry)
        self.assertNotIn("device_id", entry)

    def test_workspace_and_limit_are_validated_before_querying(self):
        with self.assertRaisesRegex(AdminServiceError, "WORKSPACE_REQUIRED"):
            self.service.overview({}, manager())
        with self.assertRaisesRegex(AuthError, "WORKSPACE_FORBIDDEN"):
            self.service.list_devices({"workspace_id": "workspace-b"}, manager())
        with self.assertRaisesRegex(AdminServiceError, "LIMIT_INVALID"):
            self.service.list_audit({"workspace_id": "workspace-a", "limit": 101}, manager())


class MutationConnection(AdminConnection):
    def transaction(self):
        return self

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if "FROM workspace_bindings AS b" in normalized and "FOR UPDATE OF b" in normalized:
            return Cursor(
                (
                    "device-b",
                    "家庭服务器",
                    "active",
                    "Hermes",
                    "active",
                    ["memory.read_context", "memory.search"],
                    "active",
                )
            )
        if "FROM agent_installations AS a" in normalized and "FOR UPDATE OF a" in normalized:
            return Cursor(("device-b", "active", "active", 4))
        if "FROM devices AS d" in normalized and "FOR UPDATE OF d" in normalized:
            return Cursor(("active", 5))
        if "UPDATE agent_installations" in normalized and "RETURNING auth_epoch" in normalized:
            return Cursor((5,))
        if "UPDATE devices" in normalized and "RETURNING auth_epoch" in normalized:
            return Cursor((6,))
        return Cursor()


class AdminMutationTests(unittest.TestCase):
    def setUp(self):
        self.connection = MutationConnection()
        self.service = PostgresAdminService(
            "postgresql://test",
            connection_factory=lambda: self.connection,
        )

    def test_update_binding_requires_expected_state_and_writes_audit(self):
        result = self.service.update_binding(
            {
                "workspace_id": "workspace-a",
                "target_agent_installation_id": "hermes-a",
                "expected_capabilities": ["memory.search", "memory.read_context"],
                "capabilities": ["memory.search", "memory.read_context", "memory.feedback"],
                "idempotency_key": "admin-ui:binding:1",
                "confirmed_by_user": True,
            },
            manager(),
        )

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["capabilities"], ["memory.feedback", "memory.read_context", "memory.search"])
        self.assertTrue(any("UPDATE workspace_bindings" in sql for sql, _ in self.connection.executed))
        self.assertTrue(any("INSERT INTO audit_log" in sql for sql, _ in self.connection.executed))

    def test_update_binding_cannot_remove_current_admin_manage(self):
        with self.assertRaisesRegex(AuthError, "ADMIN_SELF_LOCKOUT_FORBIDDEN"):
            self.service.update_binding(
                {
                    "workspace_id": "workspace-a",
                    "target_agent_installation_id": "codex-admin",
                    "expected_capabilities": ["memory.manage", "memory.search"],
                    "capabilities": ["memory.search"],
                    "idempotency_key": "admin-ui:self:1",
                    "confirmed_by_user": True,
                },
                manager(),
            )

    def test_revoke_agent_and_device_use_epoch_and_keep_audit(self):
        agent = self.service.revoke_agent(
            {
                "workspace_id": "workspace-a",
                "target_agent_installation_id": "hermes-a",
                "expected_auth_epoch": 4,
                "idempotency_key": "admin-ui:agent:1",
                "confirmed_by_user": True,
            },
            manager(),
        )
        device = self.service.revoke_device(
            {
                "workspace_id": "workspace-a",
                "target_device_id": "device-b",
                "expected_auth_epoch": 5,
                "idempotency_key": "admin-ui:device:1",
                "confirmed_by_user": True,
            },
            manager(),
        )

        self.assertEqual(agent["status"], "revoked")
        self.assertEqual(device["status"], "revoked")
        self.assertFalse(any("DELETE " in sql for sql, _ in self.connection.executed))

    def test_current_admin_cannot_revoke_itself(self):
        with self.assertRaisesRegex(AuthError, "ADMIN_SELF_REVOKE_FORBIDDEN"):
            self.service.revoke_agent(
                {
                    "workspace_id": "workspace-a",
                    "target_agent_installation_id": "codex-admin",
                    "expected_auth_epoch": 1,
                    "idempotency_key": "admin-ui:self:2",
                    "confirmed_by_user": True,
                },
                manager(),
            )


if __name__ == "__main__":
    unittest.main()
