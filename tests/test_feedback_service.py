import unittest
from unittest.mock import patch

from agent_memory_gateway.auth import Principal
from agent_memory_gateway.feedback_service import PostgresFeedbackService
from agent_memory_gateway.metadata_store import PostgresEventLedger


def principal():
    return Principal(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        agent_installation_id="agent-a",
        workspace_ids=frozenset({"workspace-a"}),
        capabilities=frozenset({"memory.feedback"}),
        workspace_capabilities={"workspace-a": frozenset({"memory.feedback"})},
    )


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if "INSERT INTO memory_feedback_events" in query:
            return _Cursor(("fb_saved",))
        return _Cursor((1,))


class FeedbackServiceTests(unittest.TestCase):
    def test_records_alias_without_changing_memory_lifecycle(self):
        connection = _Connection()
        service = PostgresFeedbackService(
            "postgresql://not-used", connection_factory=lambda: connection
        )
        with (
            patch.object(PostgresEventLedger, "_require_binding", return_value=None),
            patch.object(service, "_require_visible_memory", return_value=None),
            patch.object(service, "_require_recall", return_value=None),
        ):
            result = service.record(
                {
                    "workspace_id": "workspace-a",
                    "memory_id": "gbrain:fact:1",
                    "recall_id": "tr_recall",
                    "action": "stale",
                    "idempotency_key": "feedback-once",
                },
                principal(),
            )

        self.assertEqual(result["action"], "outdated")
        self.assertEqual(result["status"], "recorded")
        statements = "\n".join(query for query, _params in connection.calls)
        self.assertNotIn("UPDATE memory_lifecycle", statements)
        self.assertNotIn("DELETE", statements)

    def test_rejects_unknown_action(self):
        service = PostgresFeedbackService("postgresql://not-used")
        with self.assertRaisesRegex(ValueError, "FEEDBACK_ACTION_UNSUPPORTED"):
            service.record(
                {
                    "workspace_id": "workspace-a",
                    "memory_id": "gbrain:fact:1",
                    "action": "erase",
                },
                principal(),
            )


if __name__ == "__main__":
    unittest.main()
