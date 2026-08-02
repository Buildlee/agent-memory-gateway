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


if __name__ == "__main__":
    unittest.main()
