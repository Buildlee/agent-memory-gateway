from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _field_names(value: object) -> set[str]:
    if isinstance(value, dict):
        names = set(value)
        for child in value.values():
            names.update(_field_names(child))
        return names
    if isinstance(value, list):
        return set().union(*(_field_names(child) for child in value)) if value else set()
    return set()


class DeviceInstallTests(unittest.TestCase):
    def test_windows_web_installer_has_utf8_bom_for_powershell_51(self) -> None:
        script = ROOT / "scripts" / "memory-device-install.ps1"

        self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_install_profile_is_non_sensitive_and_uses_only_supported_fields(self) -> None:
        profile = json.loads((ROOT / "examples" / "device-install-profile.example.json").read_text(encoding="utf-8"))

        self.assertEqual(set(profile), {"version", "gateway_url", "default_workspace", "device_id_prefix", "agents"})
        self.assertEqual(profile["version"], 1)
        self.assertTrue(profile["gateway_url"].startswith("https://"))
        self.assertGreaterEqual(len(profile["agents"]), 1)
        self.assertIn("openclaw", {agent["type"] for agent in profile["agents"]})
        self.assertTrue(
            all(set(agent).issubset({"type", "display_name", "installation_id_template"}) for agent in profile["agents"])
        )
        self.assertFalse(
            any(
                marker in name.lower()
                for name in _field_names(profile)
                for marker in ("secret", "credential", "token", "pairing", "password", "private", "refresh", "dsn")
            )
        )

    def test_one_command_installer_delegates_to_the_controlled_device_setup(self) -> None:
        script = (ROOT / "scripts" / "memory-device-install.ps1").read_text(encoding="utf-8")

        self.assertIn("Assert-NonSensitiveProfileValue", script)
        self.assertIn("Normalize-HttpsUrl", script)
        self.assertIn("不带账号、查询参数或片段的 HTTPS 地址", script)
        self.assertIn("Save-VerifiedReleaseArchive", script)
        self.assertIn("function Expand-SafeReleaseArchive", script)
        self.assertIn("包含越界路径，拒绝解压", script)
        self.assertIn("包含符号链接，拒绝解压", script)
        self.assertIn("$maximumEntries = 10000", script)
        self.assertIn("包含过长的文件名，拒绝解压", script)
        self.assertIn("包含特殊文件，拒绝解压", script)
        self.assertIn("包含过大的单个文件，拒绝解压", script)
        self.assertIn("发布包 SHA-256 不匹配", script)
        self.assertIn("verified_release_download", script)
        self.assertIn("$DefaultStableManifestUrl", script)
        self.assertIn('[ValidateSet("stable", "development")]', script)
        self.assertIn("Get-StableReleaseSpec", script)
        self.assertIn("Assert-SafeDownloadRedirect", script)
        self.assertIn("$DefaultMainArchiveUrl", script)
        self.assertIn("default_main_archive", script)
        self.assertIn("MEMORY_DEVICE_INSTALL_PROFILE", script)
        self.assertIn("memory-gateway\\device-install.json", script)
        self.assertIn("Get-DetectedAgentSpecs", script)
        self.assertIn("没有自动检测到 Agent", script)
        self.assertIn("需要 Python 3.10 或更高版本", script)
        self.assertIn("openclaw-$ResolvedDeviceId|openclaw|OpenClaw", script)
        self.assertIn("function ConvertTo-Hashtable", script)
        self.assertIn("function Write-Utf8NoBom", script)
        self.assertNotIn("ConvertFrom-Json -AsHashtable", script)
        self.assertIn("setup-shared-memory.ps1", script)
        self.assertIn('"agent_memory_gateway.device_runtime", "onboard"', script)
        self.assertIn('Join-Path $localDataRoot "memory-gateway\\runtime"', script)
        self.assertIn('if ($NoAutostart) { $onboardArguments += "--no-autostart" }', script)
        self.assertIn('if ($Resume) { $onboardArguments += "--resume" }', script)
        self.assertIn('[Environment]::SetEnvironmentVariable("Path"', script)
        self.assertIn("memory-device-launcher.py", script)
        self.assertIn("--force-reinstall --no-deps", script)
        self.assertIn("共享记忆设备已经安装。当前状态", script)
        self.assertIn("检测到旧版 Windows 安装", script)
        self.assertIn("配对码只在隐藏输入中使用", script)
        self.assertNotIn("[string]$PairingCode", script)

    def test_unix_installer_uses_a_bounded_archive_and_the_shared_onboard_cli(self) -> None:
        script = (ROOT / "scripts" / "memory-device-install.sh").read_text(encoding="utf-8")

        self.assertIn("MAX_ARCHIVE_BYTES=268435456", script)
        self.assertIn('CHANNEL="${MEMORY_DEVICE_CHANNEL:-stable}"', script)
        self.assertIn("release-manifest.json", script)
        self.assertIn('if [ "$CHANNEL" = "development" ]', script)
        self.assertIn("MEMORY_DEVICE_ARCHIVE_SHA256", script)
        self.assertIn("自定义 MEMORY_DEVICE_ARCHIVE_URL 必须同时提供", script)
        self.assertIn("源码包 SHA-256 不匹配", script)
        self.assertIn("源码包重定向后的地址不安全", script)
        self.assertIn("sys.version_info >= (3, 10)", script)
        self.assertIn("源码包包含不安全路径", script)
        self.assertIn("源码包包含符号链接", script)
        self.assertIn('"$RUNTIME_PYTHON" -m pip install', script)
        self.assertIn("--force-reinstall --no-deps", script)
        self.assertIn("源码包展开后超过 512 MiB", script)
        self.assertIn("源码包包含特殊文件", script)
        self.assertIn('"$MEMORY_DEVICE" onboard "$@" </dev/tty', script)
        self.assertIn('ln -s "$LAUNCHER_COMMAND" "$COMMAND_LINK"', script)
        self.assertIn('"$RUNTIME_PYTHON" "$LAUNCHER_PYTHON"', script)
        self.assertIn('[ -f "$RUNTIME_CONFIG" ] && [ "$#" -eq 0 ]', script)
        self.assertIn("memory-device status、doctor、repair、uninstall", script)
        self.assertNotIn("sudo ", script)

    def test_profile_generator_refuses_overwrite_and_only_emits_non_sensitive_defaults(self) -> None:
        script = (ROOT / "scripts" / "new-device-install-profile.ps1").read_text(encoding="utf-8")

        self.assertIn("拒绝覆盖已有安装配置", script)
        self.assertIn("GatewayUrl 必须是不带账号、查询参数或片段的 HTTPS 地址", script)
        self.assertIn("installation_id_template", script)
        self.assertIn("ReleaseArchivePath", script)
        self.assertIn("ReleaseArchiveUrl 需要 ReleaseSha256", script)
        self.assertNotIn("PairingCode", script)

    @unittest.skipUnless(shutil.which("pwsh"), "requires PowerShell 7")
    def test_runtime_rejects_sensitive_profile_before_any_install_action(self) -> None:
        profile = {
            "version": 1,
            "gateway_url": "https://memory-gateway.example.internal",
            "default_workspace": "shared-workspace",
            "refresh_credential": "not-a-real-credential",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device-install.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            result = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(ROOT / "scripts" / "memory-device-install.ps1"),
                    "-ProfilePath",
                    str(path),
                    "-Plan",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refresh_credential", (result.stdout or "") + (result.stderr or ""))

    @unittest.skipUnless(shutil.which("pwsh"), "requires PowerShell 7")
    def test_runtime_rejects_profile_url_with_query_parameters(self) -> None:
        profile = {
            "version": 1,
            "gateway_url": "https://memory-gateway.example.internal/?v=1",
            "default_workspace": "shared-workspace",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device-install.json"
            path.write_text(json.dumps(profile), encoding="utf-8")
            result = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(ROOT / "scripts" / "memory-device-install.ps1"),
                    "-ProfilePath",
                    str(path),
                    "-Plan",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gateway_url", (result.stdout or "") + (result.stderr or ""))

    @unittest.skipUnless(shutil.which("pwsh"), "requires PowerShell 7")
    def test_standalone_bootstrap_plan_requires_a_verified_release_spec_without_downloading(self) -> None:
        profile = {
            "version": 1,
            "gateway_url": "https://memory-gateway.example.internal",
            "default_workspace": "shared-workspace",
            "agents": [{"type": "other", "display_name": "Test Agent"}],
            "release": {
                "release_id": "agent-memory-gateway-test",
                "archive_url": "https://releases.example.internal/agent-memory-gateway.zip",
                "sha256": "a" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / "memory-device-install.ps1"
            shutil.copy2(ROOT / "scripts" / "memory-device-install.ps1", bootstrap)
            profile_path = root / "device-install.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            result = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(bootstrap),
                    "-ProfilePath",
                    str(profile_path),
                    "-Plan",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified_release_download", result.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "requires PowerShell 7")
    def test_standalone_bootstrap_plan_defaults_to_stable_release_without_downloading(self) -> None:
        profile = {
            "version": 1,
            "gateway_url": "https://memory-gateway.example.internal",
            "default_workspace": "shared-workspace",
            "agents": [{"type": "other", "display_name": "Test Agent"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / "memory-device-install.ps1"
            shutil.copy2(ROOT / "scripts" / "memory-device-install.ps1", bootstrap)
            profile_path = root / "device-install.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            result = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(bootstrap),
                    "-ProfilePath",
                    str(profile_path),
                    "-Plan",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stable_release_download", result.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "requires PowerShell 7")
    def test_profile_generator_calculates_the_release_archive_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "agent-memory-gateway.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("agent-memory-gateway/pyproject.toml", "[project]\nname='test'\n")
            profile_path = root / "device-install.json"
            result = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(ROOT / "scripts" / "new-device-install-profile.ps1"),
                    "-GatewayUrl",
                    "https://memory-gateway.example.internal",
                    "-DefaultWorkspace",
                    "shared-workspace",
                    "-ReleaseArchiveUrl",
                    "https://releases.example.internal/agent-memory-gateway.zip",
                    "-ReleaseArchivePath",
                    str(archive_path),
                    "-ReleaseId",
                    "agent-memory-gateway-test",
                    "-OutputPath",
                    str(profile_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(profile["release"]["release_id"], "agent-memory-gateway-test")
        self.assertEqual(profile["release"]["sha256"], archive_hash)


if __name__ == "__main__":
    unittest.main()
