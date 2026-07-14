import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.gateway import GatewayHandler, ThreadingHTTPServer


class GatewayHealthTests(unittest.TestCase):
    def setUp(self):
        self._previous_probe = GatewayHandler.readiness_probe
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._url = f"http://127.0.0.1:{self._server.server_port}"

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        GatewayHandler.readiness_probe = self._previous_probe

    def _get_json(self, path):
        with urlopen(self._url + path, timeout=2) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_live_and_legacy_health_are_available(self):
        for path in ("/health/live", "/v1/health"):
            status, payload = self._get_json(path)
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])

    def test_ready_reflects_dependency_probe(self):
        GatewayHandler.readiness_probe = lambda: None
        status, payload = self._get_json("/health/ready")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "postgres")

        GatewayHandler.readiness_probe = lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
        with self.assertRaises(HTTPError) as context:
            self._get_json("/health/ready")
        self.assertEqual(context.exception.code, 503)
