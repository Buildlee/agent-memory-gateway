from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CentralAdminSetupTests(unittest.TestCase):
    def test_pairing_container_keeps_standard_input_open(self) -> None:
        script = (ROOT / "scripts" / "setup-central-admin.ps1").read_text(encoding="utf-8")

        self.assertIn('printf \'%s\\n\' "$pairing_code" | docker run --rm -i --network', script)
        self.assertIn('--pairing-code-stdin', script)
        self.assertIn('require_owner_only_file()', script)
        self.assertIn('600|700)', script)

    def test_central_containers_accept_owner_only_nas_modes(self) -> None:
        compose = (ROOT / "deploy" / "fn" / "admin-console.compose.yaml").read_text(encoding="utf-8")

        self.assertEqual(compose.count('case "$(stat -c %a'), 2)
        self.assertEqual(compose.count('600|700)'), 2)
        self.assertNotIn('stat -c %a /state/sidecar.env)" = "600"', compose)
        self.assertNotIn('stat -c %a /run/secrets/admin-sidecar.env)" = "600"', compose)


if __name__ == "__main__":
    unittest.main()
