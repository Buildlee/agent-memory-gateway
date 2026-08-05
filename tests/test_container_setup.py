from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ContainerSetupTests(unittest.TestCase):
    def test_container_setup_uses_the_generic_mcp_bridge_contract(self) -> None:
        script = (ROOT / "scripts" / "setup-container-sidecar.ps1").read_text(encoding="utf-8")
        compose = (ROOT / "deploy" / "fn" / "memory-mcp-bridge.compose.yaml").read_text(encoding="utf-8")

        self.assertIn("ClientContainerName", script)
        self.assertIn("memory-gateway bind-workspace", script)
        self.assertIn("--network \"container:$client_container\"", script)
        self.assertIn("http://127.0.0.1:8767/mcp", script)
        self.assertIn("set -e\nset -u", script)
        self.assertIn("RedirectStandardInput", script)
        self.assertIn("-replace \"`r`n\", \"`n\"", script)
        self.assertIn('("__" + $name + "__")', script)
        self.assertIn('MEMORY_SIDECAR_UID="${container_user%%:*}"', script)
        self.assertIn('MEMORY_SIDECAR_GID="${container_user##*:}"', script)
        self.assertIn('uid="${container_user%%:*}"', script)
        self.assertIn('gid="${container_user##*:}"', script)
        self.assertNotIn(r'\${container_user', script)
        self.assertIn('gateway_entrypoint="$(docker inspect "$gateway_container"', script)
        self.assertIn('label=com.docker.compose.service=app', script)
        self.assertIn('label=com.docker.compose.service=gateway', script)
        self.assertIn('[string]$GatewayInternalUrl = "http://app:8787"', script)
        self.assertIn('gateway_service="$(docker inspect "$gateway_container"', script)
        self.assertIn('gateway_url="http://$gateway_service:8787"', script)
        self.assertIn('Gateway 与目标 Agent 容器没有共同的 Docker 网络', script)
        self.assertNotIn('gateway_ip=', script)
        self.assertIn('client_status="$(docker inspect "$client_container"', script)
        self.assertIn('bridge_status=absent', script)
        self.assertIn('目标 Agent 容器当前未运行', script)
        self.assertIn('"$gateway_entrypoint" memory-gateway pairing-code', script)
        self.assertIn('--workspace-id "$workspace_id" --capabilities "$capabilities"', script)
        self.assertIn('"$gateway_entrypoint" memory-gateway bind-workspace', script)
        self.assertIn('if [ "$paired_with_workspace" != 1 ]; then', script)
        self.assertIn('docker container inspect "$key_container"', script)
        self.assertIn('key_container="${key_container}-$(date +%s)"', script)
        self.assertIn("--force-recreate memory-mcp-bridge", script)
        self.assertIn('docker run --name "$pair_container"', script)
        self.assertNotIn("docker run --rm", script)
        self.assertIn("network_mode: \"service:${MEMORY_CLIENT_SERVICE", compose)
        self.assertIn("MEMORY_REFRESH_CREDENTIAL_FILE", compose)
        self.assertIn("MEMORY_AGENT_INSTALLATION_ID", compose)
        self.assertIn("command:\n      - >-", compose)
        self.assertIn("exec python -m agent_memory_gateway.sidecar_mcp", compose)
        self.assertIn("mcp_sync_status=ready", script)
        self.assertIn('if [ ! -e "$bridge_env" ] || [ "$resume" = 1 ]; then', script)
        self.assertNotIn("hermes-webui", script)

    def test_reconciler_recreates_only_a_stale_bridge_owned_by_the_client_compose_project(self) -> None:
        script = (ROOT / "scripts" / "reconcile-container-sidecar.ps1").read_text(encoding="utf-8")

        self.assertIn("ClientContainerName", script)
        self.assertIn("StateDirectory", script)
        self.assertIn("status=waiting_for_apply", script)
        self.assertIn('label=com.docker.compose.service=memory-mcp-bridge', script)
        self.assertIn('bridge_network_mode="$(docker inspect "$bridge_id"', script)
        self.assertIn('[ "$bridge_network_mode" = "container:$client_id" ]', script)
        self.assertIn('gateway_url="http://$gateway_service:8787"', script)
        self.assertIn("--force-recreate memory-mcp-bridge", script)
        self.assertNotIn("--remove-orphans", script)
        self.assertNotIn("docker system prune", script)


if __name__ == "__main__":
    unittest.main()
