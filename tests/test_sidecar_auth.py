import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_memory_gateway.file_credential import read_file_credential, write_file_credential
from agent_memory_gateway.sidecar_auth import FileRefreshTokenProvider, WindowsRefreshTokenProvider


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class WindowsRefreshTokenProviderTests(unittest.TestCase):
    def test_refresh_rotates_credential_and_caches_short_token(self):
        provider = WindowsRefreshTokenProvider(
            "http://127.0.0.1:8787",
            "AgentMemoryGateway/test-device",
        )
        with (
            patch(
                "agent_memory_gateway.sidecar_auth.read_generic_credential",
                return_value=("test-user", "old-refresh"),
            ),
            patch("agent_memory_gateway.sidecar_auth.replace_generic_credential") as replace,
            patch(
                "agent_memory_gateway.sidecar_auth.request.urlopen",
                return_value=_Response(
                    {
                        "access_token": "short-token",
                        "refresh_credential": "new-refresh",
                        "token_type": "Bearer",
                        "expires_in": 900,
                    }
                ),
            ) as urlopen,
        ):
            self.assertEqual(provider.access_token("codex-pc"), "short-token")
            self.assertEqual(provider.access_token("codex-pc"), "short-token")

        self.assertEqual(urlopen.call_count, 1)
        replace.assert_called_once_with(
            "AgentMemoryGateway/test-device",
            "test-user",
            expected_secret="old-refresh",
            new_secret="new-refresh",
        )


@unittest.skipIf(os.name == "nt", "受限文件凭据仅在 Linux/NAS 上启用")
class FileRefreshTokenProviderTests(unittest.TestCase):
    def test_refresh_rotates_file_credential_and_caches_short_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refresh.json"
            write_file_credential(path, "fn-hermes", "old-refresh")
            provider = FileRefreshTokenProvider("http://127.0.0.1:8787", str(path))

            with patch(
                "agent_memory_gateway.sidecar_auth.request.urlopen",
                return_value=_Response(
                    {
                        "access_token": "short-token",
                        "refresh_credential": "new-refresh",
                        "token_type": "Bearer",
                        "expires_in": 900,
                    }
                ),
            ) as urlopen:
                self.assertEqual(provider.access_token("hermes-fn"), "short-token")
                self.assertEqual(provider.access_token("hermes-fn"), "short-token")

            self.assertEqual(urlopen.call_count, 1)
            self.assertEqual(read_file_credential(path), ("fn-hermes", "new-refresh"))


if __name__ == "__main__":
    unittest.main()
