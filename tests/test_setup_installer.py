from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SetupInstallerTests(unittest.TestCase):
    def test_guided_setup_reads_pairing_code_without_command_line_argument(self) -> None:
        script = (ROOT / "scripts" / "setup-shared-memory.ps1").read_text(encoding="utf-8")

        self.assertIn('Read-Host "请输入管理员生成的一次性配对码（不会显示或写入配置）" -AsSecureString', script)
        self.assertIn("SecureStringToBSTR", script)
        self.assertIn('"--pairing-code-stdin"', script)
        self.assertNotIn("[string]$PairingCode", script)

    def test_guided_setup_has_explicit_server_apply_and_non_overwriting_outputs(self) -> None:
        script = (ROOT / "scripts" / "setup-shared-memory.ps1").read_text(encoding="utf-8")

        self.assertIn("[switch]$Apply", script)
        self.assertIn('status = "waiting_for_apply"', script)
        self.assertIn("MCP 配置已存在，拒绝覆盖", script)
        self.assertIn("计划任务已存在，拒绝覆盖", script)
        self.assertIn("ReplaceExistingManagedTask = [bool]$UseExistingCredential", script)
        self.assertIn('"refreshed_and_running"', script)
        self.assertIn("UseExistingCredential", script)
        self.assertIn("必须保留原设备私钥", script)
        self.assertIn("共享记忆运行环境不完整", script)
        self.assertIn('".shared-memory-venv"', script)
        self.assertIn('& $bootstrapPython -m venv $runtimeRoot | Out-Host', script)
        self.assertIn('& $runtimePython -m pip install --disable-pip-version-check -e "$projectRoot[mcp]" | Out-Host', script)
        self.assertIn('return $runtimePython', script)
        self.assertIn("Test-SidecarHealth", script)
        self.assertIn("function Invoke-SidecarGatewaySync", script)
        self.assertIn("Sidecar 已启动，但 Gateway 同步验证失败", script)
        self.assertIn("gateway_sync = $gatewaySyncStatus", script)
        self.assertIn('status = if ($InstallAutostart) { "ready" } else { "configured" }', script)
        self.assertIn('(Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State -eq "Running"', script)
        self.assertIn('"container"', script)
        self.assertIn("setup-container-sidecar.ps1", script)

    def test_sidecar_accepts_publicly_trusted_https_without_phantom_ca_file(self) -> None:
        for name in ("start-sidecar.ps1", "install-sidecar-autostart.ps1"):
            script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn('[string]$GatewayCaCertificate = ""', script)

    def test_resume_only_replaces_a_recognized_managed_task(self) -> None:
        script = (ROOT / "scripts" / "install-sidecar-autostart.ps1").read_text(encoding="utf-8")

        self.assertIn("[switch]$ReplaceExistingManagedTask", script)
        self.assertIn("function Test-ManagedSidecarTask", script)
        self.assertIn("不是本安装器创建的受管 Sidecar 任务，拒绝替换", script)
        self.assertIn("Stop-ScheduledTask -TaskName $TaskName", script)
        self.assertIn("受管 Sidecar 任务未在 5 秒内停止，拒绝替换", script)
        self.assertIn("Register-ScheduledTask @registration -Force", script)
        self.assertIn("New-ScheduledTaskTrigger -Once", script)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 5)", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)


if __name__ == "__main__":
    unittest.main()
