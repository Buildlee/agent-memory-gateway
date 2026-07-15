import hashlib
import json
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_memory_gateway.auth import Principal, TokenAuthenticator
from agent_memory_gateway.gateway import GatewayHandler, ThreadingHTTPServer


class FakeAdminService:
    def __init__(self):
        self.payloads = []

    def overview(self, payload, principal):
        self.payloads.append((payload, principal.agent_installation_id))
        return {"workspace_id": payload["workspace_id"], "counts": {"pending_reviews": 0}}

    def list_devices(self, payload, principal):
        self.payloads.append((payload, principal.agent_installation_id))
        return {"workspace_id": payload["workspace_id"], "devices": []}

    def list_audit(self, payload, principal):
        self.payloads.append((payload, principal.agent_installation_id))
        return {"workspace_id": payload["workspace_id"], "entries": []}

    def list_dead_letters(self, payload, principal):
        self.payloads.append((payload, principal.agent_installation_id))
        return {"workspace_id": payload["workspace_id"], "dead_letters": []}


class GatewayAdminTests(unittest.TestCase):
    def setUp(self):
        self._previous = {
            "authenticator": getattr(GatewayHandler, "authenticator", None),
            "admin_service": GatewayHandler.admin_service,
            "readiness_probe": GatewayHandler.readiness_probe,
        }
        self.token = "test-admin-token"
        self.principal = Principal(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-a",
            agent_installation_id="codex-admin",
            workspace_ids=frozenset({"workspace-a"}),
            capabilities=frozenset({"memory.manage"}),
        )
        token_hash = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        GatewayHandler.authenticator = TokenAuthenticator({token_hash: self.principal})
        self.admin_service = FakeAdminService()
        GatewayHandler.admin_service = self.admin_service
        GatewayHandler.readiness_probe = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        GatewayHandler.authenticator = self._previous["authenticator"]
        GatewayHandler.admin_service = self._previous["admin_service"]
        GatewayHandler.readiness_probe = self._previous["readiness_probe"]

    def post(self, path, payload, token=None):
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {token or self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=2) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_admin_overview_requires_manage_capability_and_routes_payload(self):
        status, payload = self.post("/v1/admin/overview", {"workspace_id": "workspace-a"})

        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["pending_reviews"], 0)
        self.assertEqual(self.admin_service.payloads, [({"workspace_id": "workspace-a"}, "codex-admin")])

    def test_admin_list_routes_are_available_to_managers(self):
        _, devices = self.post("/v1/admin/devices/list", {"workspace_id": "workspace-a"})
        _, audit = self.post("/v1/admin/audit/list", {"workspace_id": "workspace-a", "limit": 10})
        _, dead_letters = self.post(
            "/v1/admin/dead-letters/list", {"workspace_id": "workspace-a", "limit": 10}
        )

        self.assertEqual(devices["devices"], [])
        self.assertEqual(audit["entries"], [])
        self.assertEqual(dead_letters["dead_letters"], [])

    def test_admin_overview_rejects_principal_without_management_capability(self):
        no_manage = Principal(
            tenant_id="tenant-a",
            user_id="user-a",
            device_id="device-b",
            agent_installation_id="codex-reader",
            workspace_ids=frozenset({"workspace-a"}),
            capabilities=frozenset({"memory.search"}),
        )
        token = "test-reader-token"
        GatewayHandler.authenticator = TokenAuthenticator(
            {hashlib.sha256(token.encode("utf-8")).hexdigest(): no_manage}
        )
        with self.assertRaises(HTTPError) as context:
            self.post("/v1/admin/overview", {"workspace_id": "workspace-a"}, token=token)
        self.assertEqual(context.exception.code, 403)
        self.assertEqual(json.loads(context.exception.read().decode("utf-8"))["error"], "CAPABILITY_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
