import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.memory_app import (
    MemoryAppError,
    build_child_commands,
    load_sidecar_environment,
    run_supervisor,
)


class MemoryAppTests(unittest.TestCase):
    def test_loads_only_the_two_sidecar_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sidecar.env"
            path.write_text(
                "MEMORY_OUTBOX_KEY=test-key\nMEMORY_OUTBOX_KEY_VERSION=v1\n",
                encoding="utf-8",
            )
            if os.name != "nt":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            values = load_sidecar_environment(path, require_private_permissions=os.name != "nt")
        self.assertEqual(set(values), {"MEMORY_OUTBOX_KEY", "MEMORY_OUTBOX_KEY_VERSION"})

    def test_rejects_environment_injection_from_sidecar_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sidecar.env"
            path.write_text(
                "MEMORY_OUTBOX_KEY=test-key\nMEMORY_OUTBOX_KEY_VERSION=v1\nPATH=/tmp\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MemoryAppError, "MEMORY_APP_SIDECAR_STATE_INVALID"):
                load_sidecar_environment(path, require_private_permissions=False)

    def test_default_commands_include_each_required_runtime(self):
        commands = build_child_commands(
            python_executable="python",
            workspace_id="workspace-a",
            public_base_url="https://memory.example.internal:8443/admin/",
            launch_token_file="/admin-state/launch-url",
        )
        self.assertEqual(
            commands[1],
            (
                "python",
                "-m",
                "agent_memory_gateway.gateway",
                "reconcile",
                "--forever",
                "--poll-interval-seconds",
                "5",
            ),
        )
        self.assertEqual(
            {command[2] for command in commands},
            {
                "agent_memory_gateway.gateway",
                "agent_memory_gateway.sidecar_daemon",
                "agent_memory_gateway.admin_console",
            },
        )

    def test_slim_compose_contains_only_app_and_proxy_services(self):
        text = (Path(__file__).resolve().parents[1] / "deploy" / "fn" / "compose.slim.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("  app:\n", text)
        self.assertIn("  proxy:\n", text)
        self.assertNotIn("  worker:\n", text)
        self.assertNotIn("  admin-sidecar:\n", text)
        self.assertNotIn("  admin-console:\n", text)

    def test_supervisor_retries_one_child_before_stopping_healthy_siblings(self):
        events: list[str] = []

        class Process:
            def __init__(self, name: str, return_code: int | None) -> None:
                self.name = name
                self.return_code = return_code

            def poll(self):
                return self.return_code

            def terminate(self):
                events.append(f"stop:{self.name}")
                self.return_code = 0

            def wait(self, timeout=None):
                return self.return_code

            def kill(self):
                self.return_code = -9

        gateway_starts = 0

        def factory(command, *, env):
            nonlocal gateway_starts
            name = command[0]
            events.append(f"start:{name}")
            if name == "gateway":
                gateway_starts += 1
                return Process(name, 1)
            return Process(name, None)

        result = run_supervisor(
            (("gateway",), ("sidecar",)),
            environment={},
            poll_seconds=0,
            process_factory=factory,
            sleep=lambda _seconds: None,
            max_child_restarts=1,
            restart_delay_seconds=0,
        )

        self.assertEqual(result, 1)
        self.assertEqual(gateway_starts, 2)
        self.assertLess(events.index("start:gateway", 1), events.index("stop:sidecar"))

    def test_supervisor_rejects_an_invalid_restart_policy(self):
        with self.assertRaisesRegex(ValueError, "MEMORY_APP_RESTART_POLICY_INVALID"):
            run_supervisor((), environment={}, max_child_restarts=0)


if __name__ == "__main__":
    unittest.main()
