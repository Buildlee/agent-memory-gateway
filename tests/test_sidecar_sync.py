import base64
import tempfile
import unittest
from pathlib import Path

from agent_memory_gateway.crypto import EventCipher
from agent_memory_gateway.outbox import Outbox
from agent_memory_gateway.sidecar_client import GatewayHTTPError, GatewayTransportError, SidecarClient


def cipher() -> EventCipher:
    encoded = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
    return EventCipher.from_base64(encoded)


def client_with_outbox(path: Path) -> SidecarClient:
    client = SidecarClient.__new__(SidecarClient)
    client.gateway_url = "http://127.0.0.1:8787"
    client.outbox = Outbox(path, cipher())
    client.agent_id = "codex"
    client.device_id = "pc"
    client.profile = "lee"
    client.default_workspace = "workspace-a"
    client.token = "test"
    return client


class SidecarSyncTests(unittest.TestCase):
    def test_retryable_push_honors_retry_after_without_dead_letter(self):
        with tempfile.TemporaryDirectory() as directory:
            client = client_with_outbox(Path(directory) / "outbox.db")
            try:
                event = client.outbox.prepare_event(
                    {"workspace_id": "workspace-a", "content": "服务暂满后重试"}
                )
                client.outbox.enqueue(event)

                def post(path, _payload):
                    if path == "/v1/sync/push":
                        raise GatewayHTTPError(
                            "DB_POOL_EXHAUSTED",
                            status=503,
                            retryable=True,
                            retry_after_seconds=0,
                        )
                    raise GatewayTransportError("offline")

                client._post = post
                result = client.sync(workspace_id="workspace-a")
                self.assertIn("DB_POOL_EXHAUSTED", result["errors"])
                self.assertEqual(client.outbox.status_counts(), {"retryable_failed": 1})
                row = client.outbox.conn.execute(
                    "SELECT attempt_count, last_error_code FROM outbox_events_v3"
                ).fetchone()
                self.assertEqual((row[0], row[1]), (1, "DB_POOL_EXHAUSTED"))
            finally:
                client.outbox.close()

    def test_nonretryable_batch_error_enters_dead_letter_without_body(self):
        with tempfile.TemporaryDirectory() as directory:
            client = client_with_outbox(Path(directory) / "outbox.db")
            try:
                event = client.outbox.prepare_event(
                    {"workspace_id": "workspace-a", "content": "不可重试的构造事件"}
                )
                client.outbox.enqueue(event)

                def post(path, _payload):
                    if path == "/v1/sync/push":
                        raise GatewayHTTPError(
                            "SCHEMA_VERSION_UNSUPPORTED",
                            status=400,
                            retryable=False,
                        )
                    return {
                        "sync_epoch": "sync-1",
                        "policy_version": "policy-1",
                        "auth_epoch": {"device": 1, "agent": 1},
                        "memories": [],
                        "tombstones": [],
                        "next_revision": 0,
                        "has_more": False,
                        "next_cursor": None,
                        "reset_required": False,
                    }

                client._post = post
                result = client.sync(workspace_id="workspace-a")
                self.assertIn("SCHEMA_VERSION_UNSUPPORTED", result["errors"])
                self.assertEqual(client.outbox.status_counts(), {"dead_letter": 1})
                stored = client.outbox.conn.execute(
                    "SELECT last_error_code, payload_ciphertext FROM outbox_events_v3"
                ).fetchone()
                self.assertEqual(stored[0], "SCHEMA_VERSION_UNSUPPORTED")
                self.assertNotIn("不可重试的构造事件".encode("utf-8"), bytes(stored[1]))
            finally:
                client.outbox.close()

    def test_push_then_pull_commits_cache_before_cleaning_acked_event(self):
        with tempfile.TemporaryDirectory() as directory:
            client = client_with_outbox(Path(directory) / "outbox.db")
            try:
                event = client.outbox.prepare_event(
                    {"workspace_id": "workspace-a", "content": "同步后的缓存事实"}
                )
                client.outbox.enqueue(event)
                calls = []

                def post(path, payload):
                    calls.append(path)
                    if path == "/v1/sync/push":
                        self.assertEqual(payload["events"][0]["event_id"], event["event_id"])
                        return {
                            "sync_epoch": "sync-1",
                            "results": [
                                {
                                    "event_id": event["event_id"],
                                    "status": "applied",
                                    "retryable": False,
                                    "ack_id": "ack-1",
                                    "server_revision": 5,
                                    "trace_id": "tr-1",
                                }
                            ],
                            "missing_device_seq": [],
                        }
                    if path == "/v1/sync/pull":
                        return {
                            "sync_epoch": "sync-1",
                            "policy_version": "policy-1",
                            "auth_epoch": {"device": 1, "agent": 1},
                            "memories": [
                                {
                                    "memory_id": "gbrain:fact:42",
                                    "server_revision": 5,
                                    "content": "同步后的缓存事实",
                                    "content_role": "reference_data",
                                    "instruction_like": False,
                                    "status": "confirmed",
                                }
                            ],
                            "tombstones": [],
                            "next_revision": 5,
                            "has_more": False,
                            "next_cursor": None,
                            "reset_required": False,
                        }
                    raise AssertionError(path)

                client._post = post
                result = client.sync(workspace_id="workspace-a")
                self.assertEqual(calls, ["/v1/sync/push", "/v1/sync/pull"])
                self.assertEqual(result["sent"], 1)
                self.assertEqual(result["cleaned"], 0)
                self.assertEqual(result["cleanup_pending"], 1)
                self.assertEqual(result["queued"], 0)
                self.assertEqual(result["outbox_states"]["acked"], 1)
                self.assertEqual(
                    client.outbox.cache_search("workspace-a", "缓存")[0]["memory_id"],
                    "gbrain:fact:42",
                )
                self.assertEqual(client.outbox.sync_state("workspace-a")["last_seen_revision"], 5)
            finally:
                client.outbox.close()

    def test_acked_ciphertext_requires_explicit_cleanup_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            client = client_with_outbox(Path(directory) / "outbox.db")
            try:
                event = client.outbox.prepare_event(
                    {"workspace_id": "workspace-a", "content": "等待用户确认的本机密文"}
                )
                client.outbox.enqueue(event)
                client.outbox.mark_terminal(event["event_id"], {"status": "applied", "ack_id": "ack-1"})

                denied = client.cleanup_confirmed(confirmed_by_user=False)
                self.assertEqual(denied, {"status": "confirmation_required", "removed": 0, "cleanup_pending": 1})
                self.assertEqual(client.outbox.status_counts(), {"acked": 1})

                cleaned = client.cleanup_confirmed(confirmed_by_user=True)
                self.assertEqual(cleaned, {"status": "cleaned", "removed": 1, "cleanup_pending": 0})
                self.assertEqual(client.outbox.status_counts(), {})
            finally:
                client.outbox.close()

    def test_offline_context_is_explicit_and_merges_pending_local_event(self):
        with tempfile.TemporaryDirectory() as directory:
            client = client_with_outbox(Path(directory) / "outbox.db")
            try:
                client.outbox.apply_pull_page(
                    "workspace-a",
                    {
                        "sync_epoch": "sync-1",
                        "policy_version": "policy-1",
                        "auth_epoch": {"device": 1, "agent": 1},
                        "memories": [
                            {
                                "memory_id": "gbrain:fact:42",
                                "server_revision": 5,
                                "content": "离线可见的授权缓存",
                                "content_role": "reference_data",
                                "instruction_like": False,
                                "status": "confirmed",
                            }
                        ],
                        "tombstones": [],
                        "next_revision": 5,
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
                pending = client.outbox.prepare_event(
                    {"workspace_id": "workspace-a", "content": "离线待同步的本机事实"}
                )
                client.outbox.enqueue(pending)
                client._post = lambda *_: (_ for _ in ()).throw(GatewayTransportError("offline"))

                result = client.context(
                    {"workspace_id": "workspace-a", "query": "离线", "max_items": 8}
                )
                self.assertTrue(result["offline"])
                self.assertTrue(result["incomplete"])
                self.assertEqual(result["last_seen_revision"], 5)
                self.assertEqual(result["pending_local_events"], 1)
                self.assertEqual(
                    {item["status"] for item in result["memory_references"]},
                    {"pending_local", "confirmed"},
                )
            finally:
                client.outbox.close()

    def test_epoch_reset_clears_cache_and_keeps_unconfirmed_event(self):
        with tempfile.TemporaryDirectory() as directory:
            client = client_with_outbox(Path(directory) / "outbox.db")
            try:
                client.outbox.apply_pull_page(
                    "workspace-a",
                    {
                        "sync_epoch": "sync-old",
                        "policy_version": "policy-1",
                        "auth_epoch": {"device": 1, "agent": 1},
                        "memories": [
                            {
                                "memory_id": "fact-old",
                                "server_revision": 1,
                                "content": "旧缓存",
                                "content_role": "reference_data",
                                "instruction_like": False,
                                "status": "confirmed",
                            }
                        ],
                        "tombstones": [],
                        "next_revision": 1,
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
                event = client.outbox.prepare_event(
                    {"workspace_id": "workspace-a", "content": "epoch 后仍需重放"}
                )
                client.outbox.enqueue(event)

                def post(path, _payload):
                    if path == "/v1/sync/push":
                        return {
                            "sync_epoch": "sync-new",
                            "policy_version": "policy-2",
                            "reset_required": True,
                            "memories": [],
                            "tombstones": [],
                            "next_revision": 0,
                            "has_more": False,
                            "next_cursor": None,
                        }
                    return {
                        "sync_epoch": "sync-new",
                        "policy_version": "policy-2",
                        "auth_epoch": {"device": 2, "agent": 1},
                        "memories": [],
                        "tombstones": [],
                        "next_revision": 0,
                        "has_more": False,
                        "next_cursor": None,
                        "reset_required": False,
                    }

                client._post = post
                result = client.sync(workspace_id="workspace-a")
                self.assertIn("SYNC_EPOCH_MISMATCH", result["errors"])
                self.assertEqual(client.outbox.cache_search("workspace-a", ""), [])
                self.assertEqual(client.outbox.count(), 1)
                self.assertEqual(client.outbox.sync_state("workspace-a")["sync_epoch"], "sync-new")
            finally:
                client.outbox.close()


if __name__ == "__main__":
    unittest.main()
