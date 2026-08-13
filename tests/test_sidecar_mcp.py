from __future__ import annotations

import unittest
from unittest import mock

from agent_memory_gateway.sidecar_mcp import _forget_payload


class SidecarMcpContractTests(unittest.TestCase):
    def test_forget_uses_the_explicit_workspace(self) -> None:
        self.assertEqual(
            _forget_payload("memory-1", False, "workspace-a"),
            {
                "memory_id": "memory-1",
                "hard_delete": False,
                "workspace_id": "workspace-a",
            },
        )

    def test_forget_uses_the_configured_default_workspace(self) -> None:
        with mock.patch.dict(
            "os.environ", {"MEMORY_DEFAULT_WORKSPACE": "workspace-default"}, clear=True
        ):
            self.assertEqual(
                _forget_payload("memory-1", False, None)["workspace_id"],
                "workspace-default",
            )


if __name__ == "__main__":
    unittest.main()
