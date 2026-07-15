import os
import unittest
from unittest.mock import patch

from agent_memory_gateway.sidecar_mcp import _active_workspace_id


class SidecarMcpWorkspaceTests(unittest.TestCase):
    def test_uses_explicit_workspace_first(self):
        with patch.dict(os.environ, {"MEMORY_DEFAULT_WORKSPACE": "team-memory"}, clear=False):
            self.assertEqual(_active_workspace_id("project-memory"), "project-memory")

    def test_uses_sidecar_workspace_when_tool_argument_is_omitted(self):
        with patch.dict(os.environ, {"MEMORY_DEFAULT_WORKSPACE": "team-memory"}, clear=False):
            self.assertEqual(_active_workspace_id(None), "team-memory")

    def test_rejects_missing_workspace_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "WORKSPACE_ID_REQUIRED"):
                _active_workspace_id(None)


if __name__ == "__main__":
    unittest.main()
