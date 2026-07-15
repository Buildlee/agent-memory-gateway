import unittest
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import Mock, patch

from agent_memory_gateway import admin_check
from agent_memory_gateway.admin_check import _workspace_id, evaluate_overview
from agent_memory_gateway.sidecar_daemon import SidecarDaemonError


class AdminCheckTests(unittest.TestCase):
    def test_healthy_overview_is_safe_for_scheduled_monitoring(self):
        result = evaluate_overview(
            {
                "workspace_id": "workspace-a",
                "worker_heartbeat_at": "2026-07-15T12:00:00+00:00",
                "counts": {
                    "pending_reviews": 2,
                    "retryable_events": 0,
                    "unresolved_dead_letters": 0,
                    "active_devices": 1,
                },
            },
            max_heartbeat_age_seconds=90,
            now=datetime(2026, 7, 15, 12, 0, 30, tzinfo=timezone.utc),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["worker_heartbeat_age_seconds"], 30.0)

    def test_stale_heartbeat_and_queue_failures_are_reported_without_payloads(self):
        result = evaluate_overview(
            {
                "workspace_id": "workspace-a",
                "worker_heartbeat_at": "2026-07-15T12:00:00+00:00",
                "counts": {"retryable_events": 1, "unresolved_dead_letters": 2},
            },
            max_heartbeat_age_seconds=60,
            now=datetime(2026, 7, 15, 12, 2, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["problems"],
            ["WORKER_HEARTBEAT_STALE", "RETRYABLE_EVENTS_PRESENT", "DEAD_LETTERS_PRESENT"],
        )
        self.assertNotIn("payload", str(result))

    def test_workspace_must_be_explicit_or_configured(self):
        self.assertEqual(_workspace_id("workspace-a"), "workspace-a")
        with self.assertRaisesRegex(ValueError, "WORKSPACE_ID_REQUIRED"):
            _workspace_id(None)

    def test_command_reports_problem_with_nonzero_exit_and_safe_json(self):
        client = Mock()
        client.admin_overview.return_value = {
            "workspace_id": "workspace-a",
            "worker_heartbeat_at": "2026-07-15T12:00:00+00:00",
            "counts": {"retryable_events": 1, "unresolved_dead_letters": 0},
        }
        output = StringIO()
        with (
            patch.object(admin_check, "get_shared_sidecar", return_value=client),
            patch.object(admin_check, "_heartbeat_age_seconds", return_value=0.0),
            patch("sys.argv", ["memory-admin-check", "--workspace", "workspace-a"]),
            patch("sys.stdout", output),
            self.assertRaises(SystemExit) as exit_context,
        ):
            admin_check.main()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("RETRYABLE_EVENTS_PRESENT", output.getvalue())
        self.assertNotIn("payload", output.getvalue())

    def test_command_reports_local_sidecar_error_with_exit_code_two(self):
        output = StringIO()
        with (
            patch.object(
                admin_check,
                "get_shared_sidecar",
                side_effect=SidecarDaemonError("LOCAL_SIDECAR_UNAVAILABLE"),
            ),
            patch("sys.argv", ["memory-admin-check", "--workspace", "workspace-a"]),
            patch("sys.stdout", output),
            self.assertRaises(SystemExit) as exit_context,
        ):
            admin_check.main()

        self.assertEqual(exit_context.exception.code, 2)
        self.assertIn("LOCAL_SIDECAR_UNAVAILABLE", output.getvalue())


if __name__ == "__main__":
    unittest.main()
