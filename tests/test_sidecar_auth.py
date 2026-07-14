import json
import unittest
from unittest.mock import patch

from agent_memory_gateway.sidecar_auth import WindowsRefreshTokenProvider


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


if __name__ == "__main__":
    unittest.main()
