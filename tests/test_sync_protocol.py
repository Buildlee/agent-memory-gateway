import base64
import json
import unittest

from agent_memory_gateway.sync_service import (
    MAX_PUSH_BYTES,
    MAX_PUSH_EVENTS,
    MAX_PULL_LIMIT,
    SYNC_POLICY_VERSION,
    SYNC_PROTOCOL_VERSION,
    PostgresSyncService,
    SyncProtocolError,
)


class SyncProtocolConstantsTests(unittest.TestCase):
    def test_sync_protocol_version(self):
        self.assertEqual(SYNC_PROTOCOL_VERSION, 1)

    def test_sync_policy_version(self):
        self.assertIsInstance(SYNC_POLICY_VERSION, str)
        self.assertTrue(SYNC_POLICY_VERSION.startswith("2026-"))

    def test_max_push_events(self):
        self.assertEqual(MAX_PUSH_EVENTS, 100)

    def test_max_push_bytes(self):
        self.assertEqual(MAX_PUSH_BYTES, 1_048_576)

    def test_max_pull_limit(self):
        self.assertEqual(MAX_PULL_LIMIT, 100)


class DecodeCursorTests(unittest.TestCase):
    def _encode_cursor(self, epoch, workspace, revision):
        raw = json.dumps(
            {"v": 1, "epoch": epoch, "workspace": workspace, "revision": revision},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def test_valid_cursor_decodes_revision(self):
        cursor = self._encode_cursor("sync_1", "workspace-a", 42)
        result = PostgresSyncService._decode_cursor(cursor, "sync_1", "workspace-a")
        self.assertEqual(result, 42)

    def test_wrong_epoch_raises_error(self):
        cursor = self._encode_cursor("sync_1", "workspace-a", 42)
        with self.assertRaises(SyncProtocolError) as cm:
            PostgresSyncService._decode_cursor(cursor, "sync_2", "workspace-a")
        self.assertEqual(cm.exception.code, "CURSOR_INVALID")

    def test_wrong_workspace_raises_error(self):
        cursor = self._encode_cursor("sync_1", "workspace-a", 42)
        with self.assertRaises(SyncProtocolError) as cm:
            PostgresSyncService._decode_cursor(cursor, "sync_1", "workspace-b")
        self.assertEqual(cm.exception.code, "CURSOR_INVALID")

    def test_garbled_cursor_raises_error(self):
        with self.assertRaises(SyncProtocolError) as cm:
            PostgresSyncService._decode_cursor("not-a-valid-cursor!!", "sync_1", "workspace-a")
        self.assertEqual(cm.exception.code, "CURSOR_INVALID")

    def test_empty_cursor_raises_error(self):
        with self.assertRaises(SyncProtocolError) as cm:
            PostgresSyncService._decode_cursor("", "sync_1", "workspace-a")
        self.assertEqual(cm.exception.code, "CURSOR_INVALID")


if __name__ == "__main__":
    unittest.main()
