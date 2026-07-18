import tempfile
import unittest
import os
import os
import subprocess
import sys
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

    def test_module_entrypoint_creates_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sidecar.env"
            project_root = Path(__file__).resolve().parents[1]
            environment = os.environ.copy()
            source_path = str(project_root / "src")
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (source_path, environment.get("PYTHONPATH", ""))
                if value
            )
            result = subprocess.run(
                [sys.executable, "-m", "agent_memory_gateway.sidecar_key", "--output", str(path)],
                check=True,
                capture_output=True,
                text=True,
                cwd=project_root,
                env=environment,
            )
            self.assertTrue(path.is_file())
            self.assertIn("sidecar_key_file=", result.stdout)
