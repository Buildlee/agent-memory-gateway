from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_memory_gateway.device_key import generate_device_key, validate_device_key_file


@unittest.skipIf(os.name == "nt", "POSIX 文件权限仅在 Linux/NAS 上检查")
class DeviceKeyPermissionTests(unittest.TestCase):
    def test_generated_key_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.pem"
            generate_device_key(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(validate_device_key_file(path), path)

    def test_group_readable_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.pem"
            generate_device_key(path)
            path.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "PERMISSIONS"):
                validate_device_key_file(path)


if __name__ == "__main__":
    unittest.main()
