import unittest

from agent_memory_gateway.auth import AuthError, Principal
from agent_memory_gateway.gateway import require_route_capabilities


def principal(*capabilities: str) -> Principal:
    values = frozenset(capabilities)
    return Principal(
        tenant_id="tenant-a",
        user_id="user-a",
        device_id="device-a",
        agent_installation_id="agent-a",
        workspace_ids=frozenset({"workspace-a"}),
        capabilities=values,
        workspace_capabilities={"workspace-a": values},
    )


class GatewayCapabilityTests(unittest.TestCase):
    def test_sync_push_requires_sync_and_write_capabilities(self):
        require_route_capabilities(
            principal("memory.sync", "memory.write_event"),
            "/v1/sync/push",
            "workspace-a",
        )

        for capabilities in (("memory.write_event",), ("memory.sync",)):
            with self.subTest(capabilities=capabilities), self.assertRaises(AuthError):
                require_route_capabilities(
                    principal(*capabilities),
                    "/v1/sync/push",
                    "workspace-a",
                )

    def test_sync_pull_requires_sync_and_read_capabilities(self):
        require_route_capabilities(
            principal("memory.sync", "memory.read_context"),
            "/v1/sync/pull",
            "workspace-a",
        )

        for capabilities in (("memory.read_context",), ("memory.sync",)):
            with self.subTest(capabilities=capabilities), self.assertRaises(AuthError):
                require_route_capabilities(
                    principal(*capabilities),
                    "/v1/sync/pull",
                    "workspace-a",
                )

    def test_non_sync_route_keeps_existing_single_capability_rule(self):
        require_route_capabilities(
            principal("memory.search"),
            "/v1/memories/search",
            "workspace-a",
        )


if __name__ == "__main__":
    unittest.main()
