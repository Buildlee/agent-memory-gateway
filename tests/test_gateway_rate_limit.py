import os
import unittest
from unittest import mock

from agent_memory_gateway.gateway import GatewayHandler


def _handler(headers: dict[str, str], address: str = "172.20.0.2") -> GatewayHandler:
    handler = GatewayHandler.__new__(GatewayHandler)
    handler.headers = mock.MagicMock()
    handler.headers.get.side_effect = headers.get
    handler.client_address = (address, 12345)
    return handler


class GatewayRateLimitSourceTests(unittest.TestCase):
    def test_forwarded_address_is_ignored_without_explicit_trust(self) -> None:
        handler = _handler({"X-Forwarded-For": "198.51.100.10"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMORY_TRUST_PROXY_X_FORWARDED_FOR", None)
            self.assertEqual(handler._rate_limit_client_ip(), "172.20.0.2")

    def test_trusted_forwarded_address_uses_first_valid_ip(self) -> None:
        handler = _handler({"X-Forwarded-For": "198.51.100.10, 172.20.0.2"})
        with mock.patch.dict(os.environ, {"MEMORY_TRUST_PROXY_X_FORWARDED_FOR": "1"}, clear=False):
            self.assertEqual(handler._rate_limit_client_ip(), "198.51.100.10")

    def test_invalid_forwarded_address_falls_back_to_peer(self) -> None:
        handler = _handler({"X-Forwarded-For": "not-an-ip"})
        with mock.patch.dict(os.environ, {"MEMORY_TRUST_PROXY_X_FORWARDED_FOR": "1"}, clear=False):
            self.assertEqual(handler._rate_limit_client_ip(), "172.20.0.2")

    def test_auth_rate_limit_checks_global_capacity_before_client_bucket(self) -> None:
        class Limiter:
            def __init__(self, allowed: bool) -> None:
                self.allowed = allowed
                self.calls = []

            def allow(self, key, *, limit, window_seconds):
                self.calls.append((key, limit, window_seconds))
                return self.allowed

        handler = _handler({})
        global_limiter = Limiter(False)
        client_limiter = Limiter(True)
        handler.auth_global_rate_limiter = global_limiter
        handler.auth_rate_limiter = client_limiter

        self.assertFalse(handler._allow_auth_request("/v1/auth/pair"))
        self.assertEqual(global_limiter.calls, [("global:/v1/auth/pair", 50, 600)])
        self.assertEqual(client_limiter.calls, [])

    def test_auth_rate_limit_applies_the_client_policy_after_global_check(self) -> None:
        class Limiter:
            def __init__(self) -> None:
                self.calls = []

            def allow(self, key, *, limit, window_seconds):
                self.calls.append((key, limit, window_seconds))
                return True

        handler = _handler({})
        global_limiter = Limiter()
        client_limiter = Limiter()
        handler.auth_global_rate_limiter = global_limiter
        handler.auth_rate_limiter = client_limiter

        self.assertTrue(handler._allow_auth_request("/v1/auth/refresh"))
        self.assertEqual(global_limiter.calls, [("global:/v1/auth/refresh", 300, 60)])
        self.assertEqual(client_limiter.calls, [("172.20.0.2:/v1/auth/refresh", 20, 60)])


if __name__ == "__main__":
    unittest.main()
