import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from agent_memory_gateway.auth import Principal
from agent_memory_gateway.store import MemoryStore


_FINGERPRINT_KEY = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="


def _principal(**kw) -> Principal:
    return Principal(
        tenant_id=kw.get("tenant_id", "personal"),
        user_id=kw.get("user_id", "lee"),
        device_id=kw.get("device_id", "pc"),
        agent_installation_id=kw.get("agent_installation_id", "codex"),
        workspace_ids=frozenset(kw.get("workspace_ids", ["ws1"])),
        capabilities=frozenset(kw.get("capabilities", ["memory.write_event"])),
    )


class StoreScannerParityTests(unittest.TestCase):
    def test_scanner_from_env(self):
        """设置了 MEMORY_SENSITIVE_FINGERPRINT_KEY 时 scanner 有 fingerprint_key。"""
        saved = os.environ.get("MEMORY_SENSITIVE_FINGERPRINT_KEY")
        os.environ["MEMORY_SENSITIVE_FINGERPRINT_KEY"] = _FINGERPRINT_KEY
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = MemoryStore(Path(directory) / "memory.db")
                try:
                    self.assertIsNotNone(store._scanner._fingerprint_key)
                    self.assertEqual(len(store._scanner._fingerprint_key), 32)
                finally:
                    store.close()
        finally:
            if saved is None:
                del os.environ["MEMORY_SENSITIVE_FINGERPRINT_KEY"]
            else:
                os.environ["MEMORY_SENSITIVE_FINGERPRINT_KEY"] = saved

    def test_scanner_without_env(self):
        """未设置环境变量时 scanner 初始化正常（无 fingerprint_key）。"""
        saved = os.environ.pop("MEMORY_SENSITIVE_FINGERPRINT_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = MemoryStore(Path(directory) / "memory.db")
                try:
                    self.assertIsNone(store._scanner._fingerprint_key)
                finally:
                    store.close()
        finally:
            if saved is not None:
                os.environ["MEMORY_SENSITIVE_FINGERPRINT_KEY"] = saved

    def test_record_event_sensitive(self):
        """敏感内容返回 blocked_sensitive。"""
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                result = store.record_event(
                    {"content": "api_key=sk-" + "abcdefghijklmnopqrstuvwxyz", "workspace_id": "ws1"},
                    _principal(),
                )
                self.assertEqual(result["status"], "blocked_sensitive")
            finally:
                store.close()

    def test_record_event_normal(self):
        """普通内容返回 event_id 和 memory。"""
        with tempfile.TemporaryDirectory() as directory:
            store = MemoryStore(Path(directory) / "memory.db")
            try:
                result = store.record_event(
                    {"content": "普通笔记", "workspace_id": "ws1"},
                    _principal(),
                )
                self.assertIn("event_id", result)
                self.assertIsNotNone(result["memory"])
                self.assertEqual(result["memory"]["content"], "普通笔记")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
