import base64
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from agent_memory_gateway.sidecar_daemon import (
    LocalSidecarProxy,
    SidecarDaemonError,
    create_sidecar_server,
    daemon_auth_token,
)
from agent_memory_gateway.sidecar_client import GatewayHTTPError


class FakeClient:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.token = None
        self.agent_id = None
        self.calls = []

    def remember(self, payload):
        self.calls.append((self.agent_id, self.token))
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        with self.lock:
            self.active -= 1
        return {"status": "queued", "content": payload.get("content")}

    def search(self, payload):
        return {"memories": [], "query": payload.get("query")}

    def context(self, payload):
        return {"memory_references": [], "query": payload.get("query")}

    def feedback(self, payload):
        return {"ok": True, "payload": payload}

    def forget(self, payload):
        return {"ok": True, "payload": payload}

    def sync(self, workspace_id=None):
        return {"queued": 0, "workspace_id": workspace_id}

    def cleanup_confirmed(self, *, confirmed_by_user):
        return {"status": "cleaned" if confirmed_by_user else "confirmation_required"}

    def admin_overview(self, payload):
        return {"workspace_id": payload["workspace_id"], "counts": {"pending_reviews": 0}}

    def list_admin_devices(self, payload):
        return {"workspace_id": payload["workspace_id"], "devices": []}

    def list_admin_audit(self, payload):
        return {"workspace_id": payload["workspace_id"], "entries": []}

    def list_admin_dead_letters(self, payload):
        return {"workspace_id": payload["workspace_id"], "dead_letters": []}


class FakeTokenProvider:
    def __init__(self):
        self.agent_ids = []

    def access_token(self, agent_installation_id):
        self.agent_ids.append(agent_installation_id)
        return f"token-for-{agent_installation_id}"


class SidecarDaemonTests(unittest.TestCase):
    def setUp(self):
        key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
        self.token = daemon_auth_token(key)
        self.client = FakeClient()
        self.server = create_sidecar_server(self.client, self.token, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.proxy = LocalSidecarProxy(
            f"http://127.0.0.1:{self.server.server_port}", self.token
        )

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def test_proxy_calls_existing_single_sidecar(self):
        self.assertTrue(self.proxy.health())
        result = self.proxy.remember({"content": "通过共用 Sidecar"})
        self.assertEqual(result["content"], "通过共用 Sidecar")
        self.assertEqual(self.proxy.sync("workspace-a")["workspace_id"], "workspace-a")

    def test_wrong_local_token_is_rejected(self):
        wrong = LocalSidecarProxy(
            f"http://127.0.0.1:{self.server.server_port}", "0" * 64
        )
        self.assertFalse(wrong.health())
        with self.assertRaises(SidecarDaemonError):
            wrong.search({"query": "x"})

    def test_gateway_error_code_is_not_hidden_as_local_internal_error(self):
        def raise_workspace_error(_payload):
            raise GatewayHTTPError("WORKSPACE_FORBIDDEN", status=403, retryable=False)

        self.client.search = raise_workspace_error
        with self.assertRaisesRegex(SidecarDaemonError, "WORKSPACE_FORBIDDEN"):
            self.proxy.search({"query": "x"})

    def test_proxy_forwards_read_only_admin_methods(self):
        overview = self.proxy.admin_overview({"workspace_id": "workspace-a"})
        devices = self.proxy.list_admin_devices({"workspace_id": "workspace-a"})
        audit = self.proxy.list_admin_audit({"workspace_id": "workspace-a", "limit": 10})
        dead_letters = self.proxy.list_admin_dead_letters({"workspace_id": "workspace-a", "limit": 10})

        self.assertEqual(overview["counts"]["pending_reviews"], 0)
        self.assertEqual(devices["devices"], [])
        self.assertEqual(audit["entries"], [])
        self.assertEqual(dead_letters["dead_letters"], [])

    def test_concurrent_rpc_is_serialized_at_state_owner(self):
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(
                executor.map(
                    lambda index: self.proxy.remember({"content": f"item-{index}"}),
                    range(40),
                )
            )
        self.assertEqual(len(results), 40)
        self.assertEqual(self.client.max_active, 1)

    def test_daemon_requests_short_token_for_declared_agent_only(self):
        provider = FakeTokenProvider()
        server = create_sidecar_server(
            self.client,
            self.token,
            port=0,
            token_provider=provider,
            allowed_agent_ids=frozenset({"codex-pc"}),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            proxy = LocalSidecarProxy(
                f"http://127.0.0.1:{server.server_port}", self.token, "codex-pc"
            )
            proxy.remember({"content": "通过独立 Sidecar"})
            self.assertEqual(provider.agent_ids, ["codex-pc"])
            self.assertEqual(self.client.calls[-1], ("codex-pc", "token-for-codex-pc"))
            self.assertIsNone(self.client.token)
            self.assertIsNone(self.client.agent_id)

            denied = LocalSidecarProxy(
                f"http://127.0.0.1:{server.server_port}", self.token, "hermes-pc"
            )
            with self.assertRaisesRegex(SidecarDaemonError, "LOCAL_AGENT_FORBIDDEN"):
                denied.remember({"content": "不应调用"})
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
