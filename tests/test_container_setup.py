from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContainerSetupTests(unittest.TestCase):
    def test_container_setup_uses_the_generic_mcp_bridge_contract(self) -> None:
        script = (ROOT / "scripts" / "setup-container-sidecar.ps1").read_text(encoding="utf-8")
        compose = (ROOT / "deploy" / "fn" / "memory-mcp-bridge.compose.yaml").read_text(encoding="utf-8")

        self.assertIn("ClientContainerName", script)
        self.assertIn("memory-gateway bind-workspace", script)
        self.assertIn("--network \"container:$client_container\"", script)
        self.assertIn("http://127.0.0.1:8767/mcp", script)
        self.assertIn("set -e\nset -u", script)
        self.assertIn("RedirectStandardInput", script)
        self.assertIn("-replace \"`r`n\", \"`n\"", script)
        self.assertIn('("__" + $name + "__")', script)
        self.assertIn('docker run --name "$pair_container"', script)
        self.assertNotIn("docker run --rm", script)
        self.assertIn("network_mode: \"service:${MEMORY_CLIENT_SERVICE", compose)
        self.assertIn("MEMORY_REFRESH_CREDENTIAL_FILE", compose)
        self.assertNotIn("hermes-webui", script)


if __name__ == "__main__":
    unittest.main()
