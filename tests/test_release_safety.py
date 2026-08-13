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

    def test_sidecar_autostart_discovers_powershell_7_without_hardcoding_install_channel(self) -> None:
        install_script = (ROOT / "scripts" / "install-sidecar-autostart.ps1").read_text(encoding="utf-8")

        self.assertIn('Get-Command -Name "pwsh"', install_script)
        self.assertNotIn('Microsoft\\WindowsApps\\pwsh.exe', install_script)
        self.assertIn('"-File", (Quote-TaskArgument $startScript)', install_script)

    def test_sidecar_autostart_sets_an_explicit_heartbeat_agent(self) -> None:
        start_script = (ROOT / "scripts" / "start-sidecar.ps1").read_text(encoding="utf-8")
        install_script = (ROOT / "scripts" / "install-sidecar-autostart.ps1").read_text(encoding="utf-8")
        setup_script = (ROOT / "scripts" / "setup-shared-memory.ps1").read_text(encoding="utf-8")

        for script in (start_script, install_script):
            self.assertIn('[string]$HeartbeatAgent = ""', script)
            self.assertIn("HeartbeatAgent 必须包含在 AllowedAgents 中", script)
        self.assertIn("$env:MEMORY_HEARTBEAT_AGENT = $HeartbeatAgent", start_script)
        self.assertIn('"-HeartbeatAgent", (Quote-TaskArgument $HeartbeatAgent)', install_script)
        self.assertIn("HeartbeatAgent = $agentSpecs[0].Id", setup_script)

    def test_fn_release_script_uses_the_requested_ssh_port_for_upload_and_remote_commands(self) -> None:
        script = (ROOT / "scripts" / "deploy-fn-release.ps1").read_text(encoding="utf-8")
        self.assertIn("[int]$SshPort = 22", script)
        self.assertIn("[string]$GatewayPublicName", script)
        self.assertIn("[string]$GatewayBindAddress", script)
        self.assertIn("MEMORY_GATEWAY_BIND_ADDRESS=$GatewayBindAddress", script)
        self.assertIn('$sshArguments = @("-p", [string]$SshPort, $SshHost)', script)
        self.assertIn("& ssh @sshArguments $prepareCommand", script)
        self.assertIn("& scp -P $SshPort -r", script)
        self.assertIn("[string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)", script)
        self.assertIn('[string]$DeploymentProfile = "slim"', script)
        self.assertIn("compose.slim.yaml", script)
        self.assertIn("AdminEnvironmentFile", script)
        self.assertIn("发布副本缺少必要路径", script)
        self.assertIn("up -d --no-build --force-recreate", script)

    def test_fn_image_retries_slow_package_downloads(self) -> None:
        dockerfile = (ROOT / "deploy" / "fn" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("PIP_DEFAULT_TIMEOUT=180", dockerfile)
        self.assertIn("PIP_RETRIES=5", dockerfile)
        self.assertIn("--retries 5 --timeout 180", dockerfile)

    def test_ci_runs_release_gates_without_parent_commit_assumption(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", workflow)
        self.assertIn("python -m compileall -q src tests", workflow)
        self.assertIn("sh -n scripts/memory-device-install.sh", workflow)
        self.assertIn("公开文件是否包含敏感信息", workflow)
        self.assertIn(".\\scripts\\check-public-sensitive.ps1", workflow)
        self.assertIn("git diff-tree --check -r HEAD", workflow)
        self.assertNotIn("HEAD^ HEAD", workflow)
        self.assertIn("ubuntu-latest", workflow)
        self.assertIn("macos-latest", workflow)
        self.assertIn("cross-platform:", workflow)
        self.assertIn('python-version: ["3.10", "3.11", "3.12", "3.13"]', workflow)
        self.assertIn("python scripts/build-wheel.py --outdir dist", workflow)
        self.assertIn("memory-device --help", workflow)

        scanner = (ROOT / "scripts" / "check-public-sensitive.ps1").read_text(encoding="utf-8")
        self.assertIn("git ls-files --cached --others --exclude-standard", scanner)
        self.assertIn("尚未暂存", scanner)
        self.assertIn("$_ -ne 'tests/fixtures/security_cases.json'", scanner)

        hook = (ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
        self.assertIn("check-public-sensitive.ps1", hook)

    def test_wheel_checker_rejects_temporary_modules_and_local_paths(self) -> None:
        checker = (ROOT / "scripts" / "check-wheel.py").read_text(encoding="utf-8")

        self.assertIn("_tmp", checker)
        self.assertIn("C:/Users/", checker)
        self.assertIn("memory-device = agent_memory_gateway.device_runtime:main", checker)

        builder = (ROOT / "scripts" / "build-wheel.py").read_text(encoding="utf-8")
        self.assertIn("clean_packaging_cache", builder)
        self.assertIn('"check-wheel.py"', builder)
        self.assertIn('startswith(("lib", "bdist.", "temp."))', builder)
        self.assertIn("cwd=outdir", builder)
        self.assertIn('"--no-build-isolation"', builder)
        self.assertNotIn("rmtree(ROOT", builder)

    def test_release_workflow_builds_immutable_assets_and_checksums(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        builder = (ROOT / "scripts" / "build-release.py").read_text(encoding="utf-8")

        self.assertIn('tags:', workflow)
        self.assertIn('git merge-base --is-ancestor "$GITHUB_SHA" origin/main', workflow)
        self.assertIn("python scripts/build-release.py", workflow)
        self.assertIn("sha256sum --check SHA256SUMS", workflow)
        self.assertIn('gh release create "$GITHUB_REF_NAME"', workflow)
        self.assertIn('release_flags+=(--prerelease)', workflow)
        self.assertIn("release-manifest.json", builder)
        self.assertIn("git", builder)
        self.assertIn("ls-files", builder)
        self.assertIn("refuse_existing", builder)
        self.assertIn("必须预先包含且仅包含一个已校验", builder)
        self.assertIn("archive_url", builder)

    def test_security_fixture_is_explicitly_nonworking_test_data(self) -> None:
        fixture = (ROOT / "tests" / "fixtures" / "security_cases.json").read_text(encoding="utf-8")
        self.assertIn("non-working-test-material", fixture)
        self.assertIn("non-working-password", fixture)
        self.assertIn("sk-" + "aaaaaaaaaaaaaaaaaaaaaaaa", fixture)
        self.assertIn("ghp_" + "aaaaaaaaaaaaaaaaaaaaaaaa", fixture)


if __name__ == "__main__":
    unittest.main()
