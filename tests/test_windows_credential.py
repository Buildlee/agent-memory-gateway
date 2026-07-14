import unittest
from unittest.mock import patch

from agent_memory_gateway.windows_credential import WindowsCredentialError, replace_generic_credential


class WindowsCredentialRotationTests(unittest.TestCase):
    def test_replace_requires_expected_current_secret(self):
        with (
            patch(
                "agent_memory_gateway.windows_credential.read_generic_credential",
                return_value=("test-user", "old-secret"),
            ),
            patch("agent_memory_gateway.windows_credential._write_generic_credential") as writer,
        ):
            replace_generic_credential(
                "AgentMemoryGateway/pc",
                "test-user",
                expected_secret="old-secret",
                new_secret="new-secret",
            )
            writer.assert_called_once_with("AgentMemoryGateway/pc", "test-user", "new-secret")

    def test_replace_refuses_stale_or_same_value(self):
        with patch(
            "agent_memory_gateway.windows_credential.read_generic_credential",
            return_value=("test-user", "current-secret"),
        ):
            with self.assertRaises(WindowsCredentialError):
                replace_generic_credential(
                    "AgentMemoryGateway/pc",
                    "test-user",
                    expected_secret="stale-secret",
                    new_secret="new-secret",
                )
            with self.assertRaises(WindowsCredentialError):
                replace_generic_credential(
                    "AgentMemoryGateway/pc",
                    "test-user",
                    expected_secret="current-secret",
                    new_secret="current-secret",
                )


if __name__ == "__main__":
    unittest.main()
