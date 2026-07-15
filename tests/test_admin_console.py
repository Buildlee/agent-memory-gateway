import json
import threading
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from agent_memory_gateway.admin_console import (
    LocalAdminSession,
    create_admin_console_server,
)


class FakeSidecar:
    def __init__(self):
        self.resolve_payloads = []
        self.search_payloads = []

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
        return {"workspace_id": payload["workspace_id"], "devices": []}

    def list_admin_audit(self, payload):
        return {"workspace_id": payload["workspace_id"], "entries": []}

    def list_admin_dead_letters(self, payload):
        return {"workspace_id": payload["workspace_id"], "dead_letters": []}

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
        self.assertIn('data-view="activity"', html)
        self.assertIn("LOCAL_METHOD_UNSUPPORTED", html)
        self.assertIn('aria-current="page"', html)
        self.assertIn('for="memory-query"', html)
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

    def test_read_only_pages_use_workspace_and_do_not_return_payloads(self):
        cookie = self._open_session()

        overview = self._json("/api/overview", cookie)
        health = self._json("/api/health", cookie)
        reviews = self._json("/api/reviews", cookie)
        memories = self._json("/api/memories?q=%E5%8F%91%E5%B8%83", cookie)

        self.assertEqual(overview["workspace_id"], "workspace-a")
        self.assertTrue(health["ok"])
        self.assertEqual(reviews["count"], 1)
        self.assertEqual(memories["memories"][0]["memory_id"], "gbrain:fact:1")
        self.assertEqual(
            self.sidecar.search_payloads,
            [{"workspace_id": "workspace-a", "query": "发布", "limit": 20}],
        )
        self.assertNotIn("MEMORY_OUTBOX_KEY", json.dumps([overview, health, reviews, memories], ensure_ascii=False))

    def test_memory_search_rejects_empty_or_too_short_queries(self):
        cookie = self._open_session()
        for path in ("/api/memories", "/api/memories?q=x"):
            with self.assertRaises(HTTPError) as context:
                self._json(path, cookie)
            self.assertEqual(context.exception.code, 400)
            self.assertIn("MEMORY_QUERY_INVALID", context.exception.read().decode("utf-8"))

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


if __name__ == "__main__":
    unittest.main()
