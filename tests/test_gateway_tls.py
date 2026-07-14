import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_memory_gateway.gateway_tls import (
    GatewayTLSConfigurationError,
    gateway_ssl_context,
)


class GatewayTLSTests(unittest.TestCase):
    def test_http_does_not_create_tls_context_without_explicit_ca(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "agent_memory_gateway.gateway_tls.ssl.create_default_context"
        ) as factory:
            self.assertIsNone(gateway_ssl_context("http://127.0.0.1:8787"))
        factory.assert_not_called()

    def test_https_uses_only_explicit_ca_file(self):
        with tempfile.TemporaryDirectory() as directory:
            certificate_path = Path(directory) / "gateway-root.crt"
            certificate_path.write_text("test certificate", encoding="utf-8")
            expected_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            with (
                patch.dict(
                    os.environ,
                    {"MEMORY_GATEWAY_CA_CERTIFICATE": str(certificate_path)},
                    clear=True,
                ),
                patch(
                    "agent_memory_gateway.gateway_tls.ssl.create_default_context",
                    return_value=expected_context,
                ) as factory,
            ):
                self.assertIs(gateway_ssl_context("https://198.51.100.10:8443"), expected_context)
            factory.assert_called_once_with(cafile=str(certificate_path))

    def test_https_rejects_missing_explicit_ca_file(self):
        with patch.dict(
            os.environ,
            {"MEMORY_GATEWAY_CA_CERTIFICATE": "C:/missing/gateway-root.crt"},
            clear=True,
        ):
            with self.assertRaisesRegex(GatewayTLSConfigurationError, "GATEWAY_CA_CERTIFICATE_MISSING"):
                gateway_ssl_context("https://198.51.100.10:8443")

    def test_explicit_ca_is_rejected_for_http(self):
        with patch.dict(
            os.environ,
            {"MEMORY_GATEWAY_CA_CERTIFICATE": "C:/unused/gateway-root.crt"},
            clear=True,
        ):
            with self.assertRaisesRegex(GatewayTLSConfigurationError, "GATEWAY_CA_REQUIRES_HTTPS"):
                gateway_ssl_context("http://127.0.0.1:8787")


if __name__ == "__main__":
    unittest.main()
