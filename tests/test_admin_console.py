import json
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from agent_memory_gateway.admin_console import (
    AdminConsoleError,
    LocalAdminSession,
    _load_or_create_session_secret,
    create_admin_console_server,
)


class FakeSidecar:
    def __init__(self):
        self.resolve_payloads = []
        self.search_payloads = []
        self.binding_payloads = []
        self.revoked_agent_payloads = []
        self.revoked_device_payloads = []

    def admin_overview(self, payload):
        return {
            "workspace_id": payload["workspace_id"],
            "worker_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "pending_reviews": 1,
                "retryable_events": 0,
                "unresolved_dead_letters": 0,
                "active_devices": 1,
            },
        }

    def list_reviews(self, payload):
        return {
            "reviews": [
                {
                    "review_id": "rv_1",
                    "revision": 2,
                    "kind": "note",
                    "scope": "workspace",
                    "content": "确认后的长期偏好",
                    "metadata": {"entity_key": "project"},
                    "instruction_like": False,
                    "conflicts": [],
                }
            ],
            "count": 1,
        }

    def list_admin_devices(self, payload):
        return {
            "workspace_id": payload["workspace_id"],
            "capability_catalog": [
                "memory.feedback",
                "memory.manage",
                "memory.read_context",
                "memory.search",
            ],
            "devices": [
                {
                    "device_id": "device-a",
                    "device_name": "FN Hermes",
                    "device_type": "nas",
                    "device_status": "active",
                    "device_auth_epoch": 3,
                    "device_last_seen_at": "2026-07-19T01:00:00+00:00",
                    "agent_installation_id": "hermes-fn",
                    "agent_name": "Hermes on FN",
                    "agent_type": "hermes",
                    "agent_auth_epoch": 2,
                    "binding_status": "bound",
                    "binding_updated_at": "2026-07-19T00:55:00+00:00",
                    "capabilities": ["memory.search", "memory.read_context"],
                    "is_current_device": False,
                    "is_current_agent": False,
                }
            ],
        }

    def list_admin_audit(self, payload):
        return {"workspace_id": payload["workspace_id"], "entries": []}

    def list_admin_dead_letters(self, payload):
        return {"workspace_id": payload["workspace_id"], "dead_letters": []}

    def memory_impact(self, payload):
        return {
            "workspace_id": payload["workspace_id"],
            "summary": {"recall_count_24h": 2, "recalled_items_24h": 4, "feedback_count_30d": 1, "positive_rate_30d": 1.0},
            "agents": [],
            "recent_feedback": [],
        }

    def list_memory_sources(self, payload):
        return {"workspace_id": payload["workspace_id"], "sources": [], "recent_bindings": []}

    def search(self, payload):
        self.search_payloads.append(payload)
        return {
            "workspace_id": payload["workspace_id"],
            "memories": [
                {
                    "memory_id": "gbrain:fact:1",
                    "content": "已经确认的发布流程",
                    "kind": "decision",
                    "confidence": 0.94,
                    "scope": "workspace",
                    "status": "confirmed",
                }
            ],
            "retrieval": {"candidate_count": 1},
        }

    def resolve_review(self, payload):
        self.resolve_payloads.append(payload)
        return {"status": "confirmed", "operation_id": "rvop_1"}

    def revert_review(self, payload):
        return {"status": "reverted", "operation_id": "rvop_2"}

    def rebuild_crystal(self, payload):
        return {"status": "queued", "workspace_id": payload["workspace_id"]}

    def update_admin_binding(self, payload):
        self.binding_payloads.append(payload)
        return {"status": "updated", "capabilities": payload["capabilities"]}

    def revoke_admin_agent(self, payload):
        self.revoked_agent_payloads.append(payload)
        return {
            "status": "revoked",
            "agent_installation_id": payload["target_agent_installation_id"],
        }

    def revoke_admin_device(self, payload):
        self.revoked_device_payloads.append(payload)
        return {"status": "revoked", "device_id": payload["target_device_id"]}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AdminConsoleTests(unittest.TestCase):
    def setUp(self):
        self.sidecar = FakeSidecar()
        self.session = LocalAdminSession(launch_token="launch-token", session_token="session-token")
        self.server = create_admin_console_server(
            workspace_id="workspace-a",
            port=0,
            sidecar_factory=lambda: self.sidecar,
            session=self.session,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _open_session(self):
        request = Request(self.url + "/?session=launch-token", method="GET")
        opener = build_opener(NoRedirect)
        with self.assertRaises(HTTPError) as context:
            opener.open(request, timeout=2)  # noqa: S310
        self.assertEqual(context.exception.code, 303)
        return context.exception.headers["Set-Cookie"].split(";", 1)[0]

    def _json(self, path, cookie, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.url + path,
            data=data,
            headers={
                "Cookie": cookie,
                "Content-Type": "application/json",
            },
            method="POST" if payload is not None else "GET",
        )
        with urlopen(request, timeout=2) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def test_launch_token_is_exchanged_for_cookie_once(self):
        cookie = self._open_session()

        html = urlopen(Request(self.url + "/", headers={"Cookie": cookie}), timeout=2).read().decode("utf-8")  # noqa: S310
        self.assertIn("Memory Admin", html)
        self.assertIn('id="confirm-dialog"', html)
        self.assertIn('data-view="memories"', html)
        self.assertIn('data-view="impact"', html)
        self.assertIn('data-view="sources"', html)
        self.assertIn('data-view="activity"', html)
        self.assertIn("LOCAL_METHOD_UNSUPPORTED", html)
        self.assertIn("Promise.allSettled", html)
        self.assertIn("部分管理信息暂时未加载", html)
        self.assertIn('aria-current="page"', html)
        self.assertIn('for="memory-query"', html)
        self.assertIn('class="metric-button"', html)
        self.assertIn('data-view-link="${escapeHTML(view)}"', html)
        self.assertIn("function stateBadge", html)
        self.assertIn("查看技术标识", html)
        self.assertIn('data-device-action="save-binding"', html)
        self.assertIn('id="activity-query"', html)
        self.assertIn("来源设备与 Agent", html)
        self.assertIn("max-width: none", html)
        self.assertNotIn("window.confirm", html)
        self.assertNotIn("launch-token", html)
        self.assertNotIn("session-token", html)

        with self.assertRaises(HTTPError) as context:
            urlopen(self.url + "/?session=launch-token", timeout=2)  # noqa: S310
        self.assertEqual(context.exception.code, 401)

    def test_api_requires_local_admin_session(self):
        with self.assertRaises(HTTPError) as context:
            urlopen(self.url + "/api/overview", timeout=2)  # noqa: S310
        self.assertEqual(context.exception.code, 401)

        with self.assertRaises(HTTPError) as page_context:
            urlopen(self.url + "/", timeout=2)  # noqa: S310
        self.assertEqual(page_context.exception.code, 401)
        self.assertIn("需要授权此浏览器", page_context.exception.read().decode("utf-8"))

    def test_read_only_pages_use_workspace_and_do_not_return_payloads(self):
        cookie = self._open_session()

        overview = self._json("/api/overview", cookie)
        health = self._json("/api/health", cookie)
        reviews = self._json("/api/reviews", cookie)
        memories = self._json("/api/memories?q=%E5%8F%91%E5%B8%83", cookie)
        impact = self._json("/api/impact", cookie)
        sources = self._json("/api/sources", cookie)

        self.assertEqual(overview["workspace_id"], "workspace-a")
        self.assertTrue(health["ok"])
        self.assertEqual(reviews["count"], 1)
        self.assertEqual(memories["memories"][0]["memory_id"], "gbrain:fact:1")
        self.assertEqual(impact["summary"]["recall_count_24h"], 2)
        self.assertEqual(sources["sources"], [])
        self.assertEqual(
            self.sidecar.search_payloads,
            [{"workspace_id": "workspace-a", "query": "发布", "limit": 50}],
        )
        self.assertNotIn("MEMORY_OUTBOX_KEY", json.dumps([overview, health, reviews, memories], ensure_ascii=False))

    def test_empty_short_queries_no_longer_reject(self):
        """空查询和过短查询不再拒绝——记忆页默认浏览全部，返回空结果即可。"""
        cookie = self._open_session()
        for path in ("/api/memories", "/api/memories?q=x"):
            resp = self._json(path, cookie)
            self.assertIsNotNone(resp)
            self.assertIn("memories", resp or {})

    def test_resolve_requires_explicit_page_confirmation(self):
        cookie = self._open_session()
        with self.assertRaises(HTTPError) as context:
            self._json(
                "/api/reviews/resolve",
                cookie,
                {
                    "review_id": "rv_1",
                    "expected_revision": 2,
                    "action": "confirm",
                    "idempotency_key": "idem-1",
                },
            )
        self.assertEqual(context.exception.code, 400)
        self.assertIn("USER_CONFIRMATION_REQUIRED", context.exception.read().decode("utf-8"))

    def test_resolve_forwards_safe_payload_through_sidecar(self):
        cookie = self._open_session()
        result = self._json(
            "/api/reviews/resolve",
            cookie,
            {
                "review_id": "rv_1",
                "expected_revision": 2,
                "action": "confirm",
                "idempotency_key": "idem-1",
                "confirmed_by_user": True,
            },
        )

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(
            self.sidecar.resolve_payloads,
            [
                {
                    "workspace_id": "workspace-a",
                    "review_id": "rv_1",
                    "action": "confirm",
                    "expected_revision": 2,
                    "idempotency_key": "idem-1",
                    "confirmed_by_user": True,
                }
            ],
        )

    def test_reverse_proxy_mount_scopes_cookie_and_api_requests(self):
        session = LocalAdminSession(launch_token="mounted-launch", session_token="mounted-session")
        server = create_admin_console_server(
            workspace_id="workspace-a",
            port=0,
            sidecar_factory=lambda: self.sidecar,
            session=session,
            mount_path="/admin",
            secure_cookie=True,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            opener = build_opener(NoRedirect)
            with self.assertRaises(HTTPError) as context:
                opener.open(Request(url + "/admin/?session=mounted-launch"), timeout=2)  # noqa: S310
            self.assertEqual(context.exception.code, 303)
            self.assertEqual(context.exception.headers["Location"], "/admin/")
            cookie = context.exception.headers["Set-Cookie"]
            self.assertIn("Path=/admin", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("Max-Age=43200", cookie)
            session_cookie = cookie.split(";", 1)[0]

            html = urlopen(Request(url + "/admin/", headers={"Cookie": session_cookie}), timeout=2).read().decode("utf-8")  # noqa: S310
            self.assertIn('data-api-base="/admin"', html)
            overview = self._json_for_url(url, "/admin/api/overview", session_cookie)
            self.assertEqual(overview["workspace_id"], "workspace-a")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_network_listener_requires_explicit_opt_in(self):
        with self.assertRaises(AdminConsoleError):
            create_admin_console_server(workspace_id="workspace-a", host="0.0.0.0", port=0)

        server = create_admin_console_server(
            workspace_id="workspace-a",
            host="0.0.0.0",
            port=0,
            allow_network=True,
        )
        server.server_close()

    def test_persistent_signing_secret_keeps_cookie_valid_across_restart(self):
        clock = [1_700_000_000.0]
        with TemporaryDirectory() as directory:
            key_path = Path(directory) / "session.key"
            secret = _load_or_create_session_secret(str(key_path))
            self.assertEqual(secret, _load_or_create_session_secret(str(key_path)))
            first = LocalAdminSession(
                launch_token="first-launch",
                session_token=secret,
                max_age_seconds=30 * 86_400,
                now=lambda: clock[0],
            )
            cookie_value = first.consume_launch_token("first-launch")
            self.assertIsNotNone(cookie_value)
            restarted = LocalAdminSession(
                launch_token="second-launch",
                session_token=secret,
                max_age_seconds=30 * 86_400,
                now=lambda: clock[0],
            )
            self.assertTrue(restarted.authorized(f"memory_admin_session={cookie_value}"))
            clock[0] += 31 * 86_400
            self.assertFalse(restarted.authorized(f"memory_admin_session={cookie_value}"))

    def test_device_management_forwards_scoped_confirmed_payloads(self):
        cookie = self._open_session()
        updated = self._json(
            "/api/devices/binding",
            cookie,
            {
                "target_agent_installation_id": "hermes-fn",
                "expected_capabilities": ["memory.search", "memory.read_context"],
                "capabilities": ["memory.search", "memory.read_context", "memory.feedback"],
                "idempotency_key": "admin-ui:binding:1",
                "confirmed_by_user": True,
            },
        )
        revoked_agent = self._json(
            "/api/devices/revoke-agent",
            cookie,
            {
                "target_agent_installation_id": "hermes-fn",
                "expected_auth_epoch": 2,
                "idempotency_key": "admin-ui:agent:1",
                "confirmed_by_user": True,
            },
        )
        revoked_device = self._json(
            "/api/devices/revoke-device",
            cookie,
            {
                "target_device_id": "device-a",
                "expected_auth_epoch": 3,
                "idempotency_key": "admin-ui:device:1",
                "confirmed_by_user": True,
            },
        )

        self.assertEqual(updated["status"], "updated")
        self.assertEqual(revoked_agent["status"], "revoked")
        self.assertEqual(revoked_device["status"], "revoked")
        self.assertEqual(self.sidecar.binding_payloads[0]["workspace_id"], "workspace-a")
        self.assertTrue(self.sidecar.binding_payloads[0]["confirmed_by_user"])

    @staticmethod
    def _json_for_url(base_url, path, cookie):
        request = Request(base_url + path, headers={"Cookie": cookie}, method="GET")
        with urlopen(request, timeout=2) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
