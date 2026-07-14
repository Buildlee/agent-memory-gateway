import tempfile
import unittest
from pathlib import Path

from agent_memory_gateway.device_key import generate_device_key


class DeviceKeyTests(unittest.TestCase):
    def test_generates_key_without_overwriting_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.pem"
            public_key = generate_device_key(path)
            self.assertEqual(len(public_key), 43)
            self.assertIn(b"BEGIN PRIVATE KEY", path.read_bytes())
            with self.assertRaises(FileExistsError):
                generate_device_key(path)


if __name__ == "__main__":
    unittest.main()
