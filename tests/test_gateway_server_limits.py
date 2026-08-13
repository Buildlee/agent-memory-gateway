import socket
import io
import json
import unittest
from unittest import mock

from agent_memory_gateway.gateway import GatewayHTTPServer, GatewayHandler, _bounded_environment_number


class GatewayServerLimitTests(unittest.TestCase):
    def test_server_validates_limits(self):
        with self.assertRaisesRegex(ValueError, "MEMORY_HTTP_READ_TIMEOUT_SECONDS_INVALID"):
            GatewayHTTPServer(("127.0.0.1", 0), GatewayHandler, request_timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "MEMORY_HTTP_MAX_CONCURRENT_REQUESTS_INVALID"):
            GatewayHTTPServer(("127.0.0.1", 0), GatewayHandler, max_concurrent_requests=0)

    def test_over_capacity_response_is_a_retryable_503(self):
        left, right = socket.socketpair()
        try:
            GatewayHTTPServer._reject_over_capacity(left)
            payload = right.recv(4096)
        finally:
            left.close()
            right.close()

        self.assertIn(b"503 Service Unavailable", payload)
        self.assertIn(b'"retryable":true', payload)

    def test_environment_number_rejects_fractional_concurrency(self):
        with mock.patch.dict("os.environ", {"TEST_LIMIT": "1.5"}):
            with self.assertRaisesRegex(ValueError, "TEST_LIMIT_INVALID"):
                _bounded_environment_number("TEST_LIMIT", 2, minimum=1, maximum=10, integer=True)

    def test_internal_error_log_omits_messages_queries_and_unknown_paths(self):
        handler = GatewayHandler.__new__(GatewayHandler)
        handler.command = "POST"
        handler.path = "/secret-in-path?token=secret-in-query"
        stream = io.StringIO()

        try:
            raise RuntimeError("secret-in-message")
        except RuntimeError as exc:
            with mock.patch("sys.stderr", stream):
                handler._log_internal_error(exc, "tr_test")

        record = json.loads(stream.getvalue())
        self.assertEqual(record["path"], "unmatched")
        self.assertNotIn("secret-in", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
