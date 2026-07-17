import io
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from agent_memory_gateway.event_contract import EventValidationError
from agent_memory_gateway.gateway import MAX_REQUEST_BODY_BYTES, GatewayHandler


def _handler(headers: dict[str, str], body: bytes = b"") -> GatewayHandler:
    """创建一个最小 GatewayHandler 用于测试 _read_json。"""
    # BaseHTTPRequestHandler 初始化需要 rfile、wfile、requestline 等
    rfile = io.BytesIO(body)
    wfile = io.BytesIO()
    handler = GatewayHandler.__new__(GatewayHandler)
    handler.headers = mock.MagicMock()
    handler.headers.get.side_effect = headers.get
    handler.rfile = rfile
    handler.wfile = wfile
    handler.command = "POST"
    handler.path = "/v1/events"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST /v1/events HTTP/1.1"
    handler.close_connection = True
    return handler


class GatewayBodyLimitTests(unittest.TestCase):
    def test_constant_value(self):
        """MAX_REQUEST_BODY_BYTES == 2MB。"""
        self.assertEqual(MAX_REQUEST_BODY_BYTES, 2_097_152)

    def test_exceeds_max(self):
        """超过 2MB 抛 REQUEST_BODY_TOO_LARGE。"""
        handler = _handler({"Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)})
        with self.assertRaises(EventValidationError) as raised:
            handler._read_json()
        self.assertEqual(raised.exception.code, "REQUEST_BODY_TOO_LARGE")

    def test_non_integer_content_length(self):
        """非整数 Content-Length 抛 CONTENT_LENGTH_INVALID。"""
        handler = _handler({"Content-Length": "abc"})
        with self.assertRaises(EventValidationError) as raised:
            handler._read_json()
        self.assertEqual(raised.exception.code, "CONTENT_LENGTH_INVALID")


if __name__ == "__main__":
    unittest.main()
