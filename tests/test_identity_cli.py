import io
import json
import unittest
from unittest import mock

from agent_memory_gateway import identity_cli


class IdentityCliTests(unittest.TestCase):
    def test_pairing_code_passes_the_preapproved_workspace_grant(self) -> None:
        admin = mock.Mock()
        admin.create_pairing_code.return_value = {"pairing_code": "pair_example"}
        output = io.StringIO()
        with (
            mock.patch.object(identity_cli, "IdentityAdmin", return_value=admin),
            mock.patch("sys.stdout", output),
        ):
            identity_cli.main(
                [
                    "pairing-code",
                    "--metadata-dsn",
                    "postgres://test",
                    "--tenant-id",
                    "personal",
                    "--user-id",
                    "chlee",
                    "--device-type",
                    "windows",
                    "--agent-types",
                    "codex,hermes",
                    "--workspace-id",
                    "agent-memory-gateway",
                    "--capabilities",
                    "memory.search,memory.sync",
                ]
            )
        admin.create_pairing_code.assert_called_once_with(
            tenant_id="personal",
            user_id="chlee",
            allowed_device_type="windows",
            allowed_agent_types=("codex", "hermes"),
            ttl_seconds=600,
            workspace_id="agent-memory-gateway",
            workspace_capabilities=("memory.search", "memory.sync"),
        )
        self.assertEqual(json.loads(output.getvalue()), {"pairing_code": "pair_example"})

    def test_pairing_code_requires_workspace_and_capabilities_together(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            identity_cli.main(
                [
                    "pairing-code",
                    "--metadata-dsn",
                    "postgres://test",
                    "--tenant-id",
                    "personal",
                    "--user-id",
                    "chlee",
                    "--device-type",
                    "windows",
                    "--agent-types",
                    "codex",
                    "--workspace-id",
                    "agent-memory-gateway",
                ]
            )
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
