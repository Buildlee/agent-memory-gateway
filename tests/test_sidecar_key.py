import tempfile
import unittest
import os
from pathlib import Path

from agent_memory_gateway.sidecar_key import generate_sidecar_key_file


class SidecarKeyTests(unittest.TestCase):
    def test_key_file_is_created_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pc-sidecar.env"
            generate_sidecar_key_file(path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("MEMORY_OUTBOX_KEY=", text)
            self.assertIn("MEMORY_OUTBOX_KEY_VERSION=v1", text)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                generate_sidecar_key_file(path)
