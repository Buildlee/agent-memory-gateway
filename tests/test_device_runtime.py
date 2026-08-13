import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_memory_gateway.device_runtime import (
    DeviceRuntimeError,
    PlatformPaths,
    detect_agent_types,
    install_device,
    load_runtime_environment,
    platform_paths,
    render_launchd_plist,
    render_mcp_config,
    render_systemd_user_unit,
    render_windows_service_manifest,
    validate_profile,
    write_service_definition,
)
from agent_memory_gateway.device_key import generate_device_key
from agent_memory_gateway.file_credential import write_file_credential


def runtime_config() -> dict:
    return {
        "runtime_config_file": "/home/test/.config/memory-gateway/runtime.json",
        "python_executable": "/opt/memory/bin/python",
        "port": 8766,
    }


class DeviceRuntimeTests(unittest.TestCase):
    @staticmethod
    def _profile() -> dict:
        return {
            "version": 1,
            "gateway_url": "https://memory.example.internal",
            "default_workspace": "workspace-a",
            "device_id_prefix": "linux",
            "agents": [
                {
                    "type": "codex",
                    "display_name": "Codex",
                    "installation_id_template": "codex-{device_id}",
                }
            ],
        }

    @staticmethod
    def _paths(root: Path) -> PlatformPaths:
        return PlatformPaths(
            config_dir=root / "config",
            state_dir=root / "state",
            data_dir=root / "data",
            service_file=root / "service" / "memory-gateway-sidecar.service",
        )

    @staticmethod
    def _successful_pairer(**kwargs) -> dict:
        device_key = Path(kwargs["device_key_file"])
        if not device_key.exists():
            generate_device_key(device_key)
        write_file_credential(
            Path(kwargs["credential_file"]),
            kwargs["credential_username"],
            "test-refresh-credential",
        )
        return {"status": "paired"}

    def test_platform_paths_follow_xdg_and_macos_conventions(self):
        linux = platform_paths(
            "linux",
            {
                "HOME": "/home/test",
                "XDG_CONFIG_HOME": "/config",
                "XDG_STATE_HOME": "/state",
                "XDG_DATA_HOME": "/data",
            },
        )
        macos = platform_paths("macos", {"HOME": "/Users/test"})

        self.assertEqual(linux.config_dir, Path("/config/memory-gateway"))
        self.assertEqual(linux.state_dir, Path("/state/memory-gateway"))
        self.assertEqual(macos.service_file.name, "com.agentmemory.gateway.sidecar.plist")
        self.assertEqual(macos.data_dir, macos.config_dir)

    def test_profile_rejects_sensitive_fields_on_every_platform(self):
        with self.assertRaisesRegex(DeviceRuntimeError, "INSTALL_PROFILE_SENSITIVE_FIELD"):
            validate_profile(
                {
                    "version": 1,
                    "gateway_url": "https://memory.example.internal",
                    "default_workspace": "workspace-a",
                    "agents": [{"type": "codex", "display_name": "Codex"}],
                    "refresh_credential": "forbidden",
                }
            )

    def test_profile_requires_a_single_device_placeholder_in_agent_template(self):
        profile = self._profile()
        profile["agents"][0]["installation_id_template"] = "codex-static"
        with self.assertRaisesRegex(DeviceRuntimeError, "INSTALL_PROFILE_AGENT_TEMPLATE_INVALID"):
            validate_profile(profile)

    def test_profile_validates_optional_release_without_treating_hash_as_a_secret(self):
        profile = self._profile()
        profile["release"] = {
            "release_id": "agent-memory-gateway-v1",
            "archive_url": "https://releases.example.internal/agent-memory-gateway.zip",
            "sha256": "a" * 64,
        }
        self.assertEqual(validate_profile(profile)["release"]["sha256"], "a" * 64)

        profile["release"]["archive_url"] = "https://user@example.internal/archive.zip"
        with self.assertRaisesRegex(DeviceRuntimeError, "INSTALL_PROFILE_RELEASE_INVALID"):
            validate_profile(profile)

    def test_profile_rejects_invalid_https_ports(self):
        profile = self._profile()
        profile["gateway_url"] = "https://memory.example.internal:70000"
        with self.assertRaisesRegex(DeviceRuntimeError, "INSTALL_PROFILE_GATEWAY_URL_INVALID"):
            validate_profile(profile)

        profile = self._profile()
        profile["release"] = {
            "release_id": "agent-memory-gateway-v1",
            "archive_url": "https://releases.example.internal:70000/archive.zip",
            "sha256": "a" * 64,
        }
        with self.assertRaisesRegex(DeviceRuntimeError, "INSTALL_PROFILE_RELEASE_INVALID"):
            validate_profile(profile)

    def test_systemd_and_launchd_bind_only_loopback(self):
        systemd = render_systemd_user_unit(runtime_config())
        launchd = render_launchd_plist(runtime_config())

        for text in (systemd, launchd):
            self.assertIn("127.0.0.1", text)
            self.assertNotIn("0.0.0.0", text)
            self.assertIn("MEMORY_DEVICE_RUNTIME_CONFIG", text)
            self.assertNotIn("MEMORY_OUTBOX_KEY", text)
            self.assertNotIn("MEMORY_REFRESH_CREDENTIAL_FILE", text)
        self.assertIn("NoNewPrivileges=true", systemd)
        self.assertIn("ProcessType", launchd)

    def test_service_definition_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "memory-gateway-sidecar.service"
            result = write_service_definition(runtime_config(), "linux", output, enable=False)
            self.assertFalse(result["enabled"])
            with self.assertRaises(FileExistsError):
                write_service_definition(runtime_config(), "linux", output, enable=False)

    def test_new_macos_install_refuses_an_already_loaded_service_label(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "agent_memory_gateway.device_runtime.os.getuid", return_value=501, create=True
        ), mock.patch(
            "agent_memory_gateway.device_runtime.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ):
            output = Path(directory) / "com.agentmemory.gateway.sidecar.plist"

            with self.assertRaisesRegex(DeviceRuntimeError, "MACOS_SERVICE_ALREADY_LOADED"):
                write_service_definition(runtime_config(), "macos", output, enable=True)

    def test_new_linux_install_refuses_an_already_enabled_service_unit(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "agent_memory_gateway.device_runtime.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="/other/memory-gateway-sidecar.service"),
        ):
            output = Path(directory) / "memory-gateway-sidecar.service"

            with self.assertRaisesRegex(DeviceRuntimeError, "LINUX_SERVICE_ALREADY_ENABLED"):
                write_service_definition(runtime_config(), "linux", output, enable=True)

    def test_mcp_config_references_key_file_without_embedding_secret(self):
        config = render_mcp_config(
            python_executable="/opt/memory/bin/python",
            agent_installation_id="codex-device-a",
            workspace_id="workspace-a",
            sidecar_key_file=Path("/config/memory-gateway/sidecar.env"),
            port=8766,
        )
        serialized = json.dumps(config)

        self.assertIn("MEMORY_SIDECAR_KEY_FILE", serialized)
        self.assertNotIn("MEMORY_OUTBOX_KEY\"", serialized)
        self.assertNotIn("MEMORY_GATEWAY_URL", serialized)

    def test_openclaw_mcp_config_uses_its_nested_server_registry(self):
        config = render_mcp_config(
            python_executable="/opt/memory/bin/python",
            agent_installation_id="openclaw-device-a",
            agent_type="openclaw",
            workspace_id="workspace-a",
            sidecar_key_file=Path("/config/memory-gateway/sidecar.env"),
            port=8766,
        )

        self.assertIn("shared-memory", config["mcp"]["servers"])
        self.assertNotIn("mcp_servers", config)

    def test_agent_detection_uses_known_directories_without_reading_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".codex").mkdir()
            (home / ".openclaw").mkdir()

            detected = detect_agent_types({"HOME": str(home), "PATH": ""})

        self.assertEqual(detected, ["codex", "openclaw"])

    def test_windows_service_manifest_runs_python_directly_with_runtime_config(self):
        config = {
            "runtime_config_file": r"C:\Users\test\AppData\Local\memory-gateway\runtime.json",
            "python_executable": r"C:\Users\test\memory-gateway\python.exe",
            "port": 8766,
        }
        manifest = json.loads(render_windows_service_manifest(config))

        self.assertEqual(manifest["managed_by"], "agent-memory-gateway")
        self.assertEqual(manifest["task_name"], "MemoryGatewaySidecar")
        self.assertEqual(manifest["command"][0], config["python_executable"])
        self.assertIn("--runtime-config", manifest["command"])
        self.assertNotIn("pwsh", json.dumps(manifest).lower())

    def test_install_writes_private_runtime_and_mcp_configs_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._paths(root)
            result = install_device(
                self._profile(),
                pairing_code="one-time-code",
                platform_name="linux",
                paths=paths,
                device_id="linux-test-device",
                device_name="Linux test device",
                python_executable="/opt/memory/bin/python",
                credential_username="test-user",
                enable_autostart=False,
                pairer=self._successful_pairer,
            )

            runtime_path = Path(result["runtime_config_file"])
            runtime_text = runtime_path.read_text(encoding="utf-8")
            self.assertEqual(result["status"], "configured")
            self.assertEqual(result["client_configuration"][0]["status"], "generated_not_imported")
            self.assertIn("重启 Agent", result["next_step"])
            self.assertNotIn("test-refresh-credential", runtime_text)
            self.assertNotIn("one-time-code", runtime_text)
            self.assertTrue(paths.service_file.is_file())
            for mcp_file in result["mcp_config_files"]:
                mcp_text = Path(mcp_file).read_text(encoding="utf-8")
                self.assertIn("MEMORY_SIDECAR_KEY_FILE", mcp_text)
                self.assertNotIn("MEMORY_OUTBOX_KEY\"", mcp_text)
            if os.name != "nt":
                self.assertEqual(runtime_path.stat().st_mode & 0o777, 0o600)

            environment = load_runtime_environment(runtime_path)
            self.assertEqual(environment["MEMORY_DEVICE_ID"], "linux-test-device")
            self.assertIn("MEMORY_REFRESH_CREDENTIAL_FILE", environment)
            self.assertNotIn("MEMORY_REFRESH_CREDENTIAL_TARGET", environment)

    def test_resume_is_idempotent_and_does_not_pair_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(Path(directory))
            arguments = {
                "pairing_code": "one-time-code",
                "platform_name": "linux",
                "paths": paths,
                "device_id": "linux-test-device",
                "device_name": "Linux test device",
                "python_executable": "/opt/memory/bin/python",
                "credential_username": "test-user",
                "enable_autostart": False,
            }
            install_device(self._profile(), pairer=self._successful_pairer, **arguments)
            pairer = mock.Mock(side_effect=AssertionError("resume must not pair again"))
            credential_patch = (
                mock.patch(
                    "agent_memory_gateway.device_runtime._existing_credential",
                    return_value=True,
                )
                if os.name == "nt"
                else nullcontext()
            )
            with credential_patch:
                result = install_device(
                    self._profile(), pairer=pairer, resume=True, **arguments
                )

            self.assertTrue(result["resumed"])
            pairer.assert_not_called()

    def test_resume_recovers_a_pairing_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = self._paths(Path(directory))

            def interrupted_pairer(**kwargs):
                generate_device_key(Path(kwargs["device_key_file"]))
                raise RuntimeError("network interrupted")

            arguments = {
                "pairing_code": "one-time-code",
                "platform_name": "linux",
                "paths": paths,
                "device_id": "linux-test-device",
                "device_name": "Linux test device",
                "python_executable": "/opt/memory/bin/python",
                "credential_username": "test-user",
                "enable_autostart": False,
            }
            with self.assertRaisesRegex(RuntimeError, "network interrupted"):
                install_device(self._profile(), pairer=interrupted_pairer, **arguments)

            result = install_device(
                self._profile(), pairer=self._successful_pairer, resume=True, **arguments
            )
            self.assertEqual(result["status"], "configured")

    def test_macos_resume_bootstraps_service_if_first_enable_was_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = PlatformPaths(
                config_dir=root / "config",
                state_dir=root / "state",
                data_dir=root / "data",
                service_file=root / "service" / "com.agentmemory.gateway.sidecar.plist",
            )
            arguments = {
                "pairing_code": "one-time-code",
                "platform_name": "macos",
                "paths": paths,
                "device_id": "macos-test-device",
                "device_name": "macOS test device",
                "python_executable": "/opt/memory/bin/python",
                "credential_username": "test-user",
            }
            install_device(
                self._profile(),
                enable_autostart=False,
                pairer=self._successful_pairer,
                **arguments,
            )

            run_result = SimpleNamespace(returncode=1)
            credential_patch = (
                mock.patch(
                    "agent_memory_gateway.device_runtime._existing_credential",
                    return_value=True,
                )
                if os.name == "nt"
                else nullcontext()
            )
            with credential_patch, mock.patch(
                "agent_memory_gateway.device_runtime.os.getuid", return_value=501, create=True
            ), mock.patch(
                "agent_memory_gateway.device_runtime.subprocess.run", return_value=run_result
            ) as runner:
                result = install_device(
                    self._profile(),
                    enable_autostart=True,
                    resume=True,
                    pairer=mock.Mock(side_effect=AssertionError("resume must not pair again")),
                    verify_ready=mock.Mock(),
                    **arguments,
                )

            self.assertEqual(result["status"], "ready")
            commands = [call.args[0] for call in runner.call_args_list]
            self.assertIn(
                ["launchctl", "bootstrap", "gui/501", str(paths.service_file)],
                commands,
            )


if __name__ == "__main__":
    unittest.main()
