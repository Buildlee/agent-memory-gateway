import unittest

from agent_memory_gateway.security import SensitiveContentScanner
from agent_memory_gateway.sidecar_client import SidecarClient


class FailingOutbox:
    def prepare_event(self, _payload):
        raise AssertionError("敏感内容不应进入 prepare_event")

    def enqueue(self, _payload):
        raise AssertionError("敏感内容不应进入 outbox")


class CapturingOutbox:
    def __init__(self):
        self.payload = None

    def prepare_event(self, payload):
        self.payload = dict(payload)
        return self.payload | {"event_id": "evt-safe"}

    def enqueue(self, _payload):
        return "evt-safe"

    def count(self):
        return 1


class SidecarSecurityTests(unittest.TestCase):
    def test_sensitive_content_is_rejected_before_outbox(self):
        client = SidecarClient.__new__(SidecarClient)
        client.security_scanner = SensitiveContentScanner()
        client.outbox = FailingOutbox()
        result = client.remember(
            {
                "content": "Authorization: Bearer " + "a" * 24,
                "workspace_id": "workspace-a",
            }
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["error"], "SENSITIVE_CONTENT")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["categories"], ["bearer_token"])

    def test_remember_does_not_send_self_reported_user_identity(self):
        client = SidecarClient.__new__(SidecarClient)
        client.security_scanner = SensitiveContentScanner()
        client.outbox = CapturingOutbox()
        client.agent_id = "codex-pc"
        client.device_id = "windows-pc"
        client.default_workspace = "workspace-a"
        client.sync = lambda workspace_id=None: {"receipts": [], "offline": False, "errors": []}

        result = client.remember({"content": "已确认的项目约定", "workspace_id": "workspace-a"})

        self.assertEqual(result["event_id"], "evt-safe")
        self.assertEqual(client.outbox.payload["agent_id"], "codex-pc")
        self.assertEqual(client.outbox.payload["device_id"], "windows-pc")
        self.assertNotIn("user_id", client.outbox.payload)


if __name__ == "__main__":
    unittest.main()
