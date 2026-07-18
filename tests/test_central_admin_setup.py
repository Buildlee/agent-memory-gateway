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


if __name__ == "__main__":
    unittest.main()
