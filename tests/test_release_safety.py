from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseSafetyTests(unittest.TestCase):
    def test_local_operational_scripts_are_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("scripts/provision-fn.ps1", ignored)
        self.assertIn("scripts/verify_gbrain_lifecycle.py", ignored)

    def test_source_checkout_can_run_admin_tools_without_installed_entrypoints(self) -> None:
        for name, command in (
            ("start-admin-console.ps1", "agent_memory_gateway.admin_console"),
            ("check-admin-health.ps1", "agent_memory_gateway.admin_check"),
        ):
            script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("PYTHONPATH", script)
            self.assertIn(command, script)
            self.assertIn("MEMORY_GATEWAY_TOKEN", script)

    def test_sidecar_and_mcp_prefer_the_current_release_source(self) -> None:
        expected_modules = {
            "start-sidecar.ps1": "agent_memory_gateway.sidecar_daemon",
            "start-sidecar-mcp.ps1": "agent_memory_gateway.sidecar_mcp",
        }
        for name, module in expected_modules.items():
            script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn('Join-Path (Split-Path -Parent $PSScriptRoot) "src"', script)
            self.assertIn("PYTHONPATH", script)
            self.assertIn(module, script)

        mcp_script = (ROOT / "scripts" / "start-sidecar-mcp.ps1").read_text(encoding="utf-8")
        self.assertIn('McpExecutable -eq "memory-sidecar-mcp"', mcp_script)
        self.assertIn("PythonExecutable", mcp_script)

    def test_fn_release_script_uses_the_requested_ssh_port_for_upload_and_remote_commands(self) -> None:
        script = (ROOT / "scripts" / "deploy-fn-release.ps1").read_text(encoding="utf-8")
        self.assertIn("[int]$SshPort = 22", script)
        self.assertIn('$sshArguments = @("-p", [string]$SshPort, $SshHost)', script)
        self.assertIn("& ssh @sshArguments $prepareCommand", script)
        self.assertIn("& scp -P $SshPort -r", script)
        self.assertIn("[string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)", script)
        self.assertIn("发布副本缺少必要路径", script)

    def test_fn_image_retries_slow_package_downloads(self) -> None:
        dockerfile = (ROOT / "deploy" / "fn" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("PIP_DEFAULT_TIMEOUT=180", dockerfile)
        self.assertIn("PIP_RETRIES=5", dockerfile)
        self.assertIn("--retries 5 --timeout 180", dockerfile)

    def test_ci_runs_release_gates_without_parent_commit_assumption(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", workflow)
        self.assertIn("python -m compileall -q src tests", workflow)
        self.assertIn("公开文件是否包含敏感信息", workflow)
        self.assertIn("git diff-tree --check -r HEAD", workflow)
        self.assertIn("$_ -ne 'tests/fixtures/security_cases.json'", workflow)
        self.assertNotIn("HEAD^ HEAD", workflow)

    def test_security_fixture_is_explicitly_nonworking_test_data(self) -> None:
        fixture = (ROOT / "tests" / "fixtures" / "security_cases.json").read_text(encoding="utf-8")
        self.assertIn("non-working-test-material", fixture)
        self.assertIn("non-working-password", fixture)
        self.assertIn("sk-" + "aaaaaaaaaaaaaaaaaaaaaaaa", fixture)
        self.assertIn("ghp_" + "aaaaaaaaaaaaaaaaaaaaaaaa", fixture)


if __name__ == "__main__":
    unittest.main()
