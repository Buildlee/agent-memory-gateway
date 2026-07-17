import base64
import json
import unittest

from agent_memory_gateway.sync_service import (
    MAX_PUSH_BYTES,
    MAX_PUSH_EVENTS,
    MAX_PULL_LIMIT,
    PostgresSyncService,
    SyncProtocolError,
    SYNC_POLICY_VERSION,
    SYNC_PROTOCOL_VERSION,
)


class SyncProtocolConstantsTests(unittest.TestCase):
    def test_protocol_version(self):
        self.assertEqual(SYNC_PROTOCOL_VERSION, 1)

    def test_policy_version(self):
        self.assertEqual(SYNC_POLICY_VERSION, "2026-07-12.2")

    def test_max_push_bytes(self):
        self.assertEqual(MAX_PUSH_BYTES, 1_048_576)

    def test_max_push_events(self):
        self.assertEqual(MAX_PUSH_EVENTS, 100)

    def test_max_pull_limit(self):
        self.assertEqual(MAX_PULL_LIMIT, 100)


class SyncCursorTests(unittest.TestCase):
    @staticmethod
    def _encode(epoch: str, workspace: str, revision: int) -> str:
        raw = json.dumps(
            {"v": 1, "epoch": epoch, "workspace": workspace, "revision": revision},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def test_valid_cursor_decodes_revision(self):
        cursor = self._encode("sync_abc", "ws1", 42)
        revision = PostgresSyncService._decode_cursor(cursor, "sync_abc", "ws1")
        self.assertEqual(revision, 42)

    def test_wrong_epoch_rejected(self):
        cursor = self._encode("sync_abc", "ws1", 42)
        with self.assertRaises(SyncProtocolError) as raised:
            PostgresSyncService._decode_cursor(cursor, "sync_xyz", "ws1")
        self.assertEqual(raised.exception.code, "CURSOR_INVALID")

    def test_wrong_workspace_rejected(self):
        cursor = self._encode("sync_abc", "ws1", 42)
        with self.assertRaises(SyncProtocolError) as raised:
            PostgresSyncService._decode_cursor(cursor, "sync_abc", "ws2")
        self.assertEqual(raised.exception.code, "CURSOR_INVALID")

    def test_garbage_rejected(self):
        with self.assertRaises(SyncProtocolError) as raised:
            PostgresSyncService._decode_cursor("not-a-valid-base64!!!", "sync_abc", "ws1")
        self.assertEqual(raised.exception.code, "CURSOR_INVALID")

    def test_empty_string_rejected(self):
        with self.assertRaises(SyncProtocolError) as raised:
            PostgresSyncService._decode_cursor("", "sync_abc", "ws1")
        self.assertEqual(raised.exception.code, "CURSOR_INVALID")

    def test_invalid_version_rejected(self):
        raw = json.dumps({"v": 2, "epoch": "sync_abc", "workspace": "ws1", "revision": 1}).encode("utf-8")
        cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        with self.assertRaises(SyncProtocolError) as raised:
            PostgresSyncService._decode_cursor(cursor, "sync_abc", "ws1")
        self.assertEqual(raised.exception.code, "CURSOR_INVALID")


if __name__ == "__main__":
    unittest.main()
