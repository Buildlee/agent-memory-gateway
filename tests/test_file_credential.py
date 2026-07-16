from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_memory_gateway.file_credential import (
    FileCredentialError,
    read_file_credential,
    replace_file_credential,
    write_file_credential,
)


@unittest.skipIf(os.name == "nt", "受限文件权限仅在 Linux/NAS 上启用")
class FileCredentialTests(unittest.TestCase):
    def test_new_file_is_private_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refresh.json"
            write_file_credential(path, "fn-hermes", "refresh-one")

            self.assertEqual(read_file_credential(path), ("fn-hermes", "refresh-one"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_file_credential(path, "fn-hermes", "refresh-two")

    def test_rotation_requires_the_previous_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refresh.json"
            write_file_credential(path, "fn-hermes", "refresh-one")

            replace_file_credential(
                path,
                "fn-hermes",
                expected_secret="refresh-one",
                new_secret="refresh-two",
            )
            self.assertEqual(read_file_credential(path), ("fn-hermes", "refresh-two"))
            with self.assertRaises(FileExistsError):
                replace_file_credential(
                    path,
                    "fn-hermes",
                    expected_secret="refresh-one",
                    new_secret="refresh-three",
                )

    def test_group_or_world_readable_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refresh.json"
            write_file_credential(path, "fn-hermes", "refresh-one")
            path.chmod(0o640)

            with self.assertRaisesRegex(FileCredentialError, "PERMISSIONS"):
                read_file_credential(path)


if __name__ == "__main__":
    unittest.main()
