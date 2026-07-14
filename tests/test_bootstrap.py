import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_memory_gateway.bootstrap import BootstrapSpec, _split_capabilities


def spec(**changes):
    values = {
        "tenant_id": "personal",
        "user_id": "lee",
        "user_name": "Lee",
        "device_id": "pc",
        "device_name": "Windows PC",
        "device_type": "windows",
        "device_public_key": "ed25519-public-key",
        "agent_installation_id": "codex-pc",
        "agent_name": "Codex",
        "agent_type": "codex",
        "workspace_id": "workspace-a",
        "workspace_name": "Workspace A",
        "capabilities": ("memory.write_event",),
    }
    return BootstrapSpec(**(values | changes))


class BootstrapSpecTests(unittest.TestCase):
    def test_valid_spec(self):
        spec().validate()

    def test_public_key_and_capabilities_are_required(self):
        with self.assertRaises(ValueError):
            spec(device_public_key="").validate()
        with self.assertRaises(ValueError):
            spec(capabilities=()).validate()

    def test_capabilities_are_deduplicated(self):
        self.assertEqual(
            _split_capabilities("memory.search, memory.write_event,memory.search"),
            ("memory.search", "memory.write_event"),
        )
