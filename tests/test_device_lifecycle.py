from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_memory_gateway.device_key import generate_device_key
from agent_memory_gateway.device_lifecycle import (
    DeviceLifecycleError,
    device_status,
    diagnose_device,
    repair_device,
    rollback_device,
    uninstall_device,
    upgrade_device,
)
from agent_memory_gateway.device_runtime import PlatformPaths
from agent_memory_gateway.file_credential import write_file_credential
from agent_memory_gateway.sidecar_key import generate_sidecar_key_file


class DeviceLifecycleTests(unittest.TestCase):
    def _installation(self, root: Path) -> tuple[PlatformPaths, dict]:
        paths = PlatformPaths(
            config_dir=root / "config",
            state_dir=root / "state",
            data_dir=root / "data",
            service_file=root / "service" / "memory-gateway-sidecar.service",
        )
        secrets = paths.config_dir / "secrets"
        secrets.mkdir(parents=True)
        sidecar_key = secrets / "sidecar.env"
        device_key = secrets / "device-identity.pem"
        credential = secrets / "device-refresh.json"
        generate_sidecar_key_file(sidecar_key)
        generate_device_key(device_key)
        write_file_credential(credential, "test-user", "test-refresh-value")
        mcp = paths.data_dir / "mcp" / "codex-linux-test-mcp.json"
        mcp.parent.mkdir(parents=True)
        paths.service_file.parent.mkdir(parents=True)
        runtime = {
            "version": 1,
            "platform": "linux",
            "runtime_config_file": str(paths.config_dir / "runtime.json"),
            "python_executable": sys.executable,
            "gateway_url": "https://memory.example.internal",
            "agent_installation_ids": ["codex-linux-test"],
            "heartbeat_agent": "codex-linux-test",
            "device_id": "linux-test",
            "default_workspace": "workspace-a",
            "sidecar_key_file": str(sidecar_key),
            "device_key_file": str(device_key),
            "credential_file": str(credential),
            "memory_home": str(paths.state_dir / "sidecar-v1"),
            "port": 8766,
            "mcp_config_files": [str(mcp)],
        }
        paths.state_dir.mkdir(parents=True)
        runtime_python = paths.data_dir / "runtime" / "bin" / "python"
        runtime_python.parent.mkdir(parents=True, exist_ok=True)
        runtime_python.write_text("test runtime", encoding="utf-8")
        runtime["python_executable"] = str(runtime_python)
        from agent_memory_gateway.device_runtime import render_mcp_config

        mcp.write_text(
            json.dumps(
                render_mcp_config(
                    python_executable=str(runtime_python),
                    agent_installation_id="codex-linux-test",
                    agent_type="other",
                    workspace_id="workspace-a",
                    sidecar_key_file=sidecar_key,
                    port=8766,
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (paths.state_dir / "preserved.txt").write_text("local data", encoding="utf-8")
        (paths.config_dir / "runtime.json").write_text(
            json.dumps(runtime, ensure_ascii=False), encoding="utf-8"
        )
        from agent_memory_gateway.device_runtime import render_systemd_user_unit

        paths.service_file.write_text(render_systemd_user_unit(runtime), encoding="utf-8")
        if os.name != "nt":
            for path in (paths.config_dir / "runtime.json", mcp):
                path.chmod(0o600)
        return paths, runtime

    def test_status_reports_not_installed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = PlatformPaths(
                Path(directory) / "config",
                Path(directory) / "state",
                Path(directory) / "data",
                Path(directory) / "service",
            )
            result = device_status("linux", paths)

        self.assertEqual(result["status"], "not_installed")

    def test_doctor_reports_a_healthy_managed_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = self._installation(Path(directory))
            with mock.patch(
                "agent_memory_gateway.device_lifecycle._service_status", return_value="running"
            ), mock.patch(
                "agent_memory_gateway.device_lifecycle._health", return_value=True
            ), mock.patch(
                "agent_memory_gateway.device_lifecycle.read_file_credential",
                return_value=("test-user", "test-refresh-value"),
            ):
                result = diagnose_device("linux", paths)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(all(item["status"] == "ok" for item in result["checks"]))

    def test_repair_previews_then_recreates_only_the_missing_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, runtime = self._installation(Path(directory))
            mcp = Path(runtime["mcp_config_files"][0])
            mcp.unlink()
            with mock.patch(
                "agent_memory_gateway.device_lifecycle._service_status", return_value="running"
            ), mock.patch("agent_memory_gateway.device_lifecycle._health", return_value=True):
                preview = repair_device("linux", paths, apply=False)
                self.assertFalse(mcp.exists())
                applied = repair_device("linux", paths, apply=True)

            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["actions"], [{"action": "create_mcp_config", "target": str(mcp)}])
            self.assertEqual(applied["status"], "completed")
            config = json.loads(mcp.read_text(encoding="utf-8"))
            self.assertEqual(
                config["mcp_servers"]["shared-memory"]["env"]["MEMORY_AGENT_INSTALLATION_ID"],
                "codex-linux-test",
            )

    def test_repair_recreates_a_missing_managed_service_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = self._installation(Path(directory))
            paths.service_file.unlink()
            with mock.patch(
                "agent_memory_gateway.device_lifecycle._service_status", return_value="missing"
            ), mock.patch(
                "agent_memory_gateway.device_lifecycle._health", return_value=False
            ), mock.patch(
                "agent_memory_gateway.device_runtime.assert_service_slot_available"
            ), mock.patch(
                "agent_memory_gateway.device_runtime.enable_service_definition"
            ):
                result = repair_device("linux", paths, apply=True)

            self.assertEqual(result["actions"][0]["action"], "create_service_definition")
            self.assertTrue(paths.service_file.is_file())

    def test_default_uninstall_previews_then_preserves_credentials_and_local_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, runtime = self._installation(Path(directory))
            runtime_file = paths.config_dir / "runtime.json"
            mcp = Path(runtime["mcp_config_files"][0])
            credential = Path(runtime["credential_file"])
            state_marker = paths.state_dir / "preserved.txt"
            preview = uninstall_device(
                "linux", paths, apply=False, purge_credentials=False, purge_data=False
            )
            self.assertTrue(runtime_file.exists())

            with mock.patch("agent_memory_gateway.device_lifecycle._remove_service"):
                result = uninstall_device(
                    "linux", paths, apply=True, purge_credentials=False, purge_data=False
                )

            self.assertEqual(preview["status"], "planned")
            self.assertEqual(result["status"], "uninstalled")
            self.assertFalse(runtime_file.exists())
            self.assertFalse(mcp.exists())
            self.assertTrue(credential.exists())
            self.assertTrue(state_marker.exists())
            self.assertTrue(Path(result["backup"]).is_file())

    def test_uninstall_rejects_a_tampered_managed_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            outside = root / "unrelated.json"
            outside.write_text("keep", encoding="utf-8")
            runtime["mcp_config_files"] = [str(outside)]
            (paths.config_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

            with self.assertRaisesRegex(DeviceLifecycleError, "MCP_CONFIG_PATHS_INVALID"):
                uninstall_device(
                    "linux", paths, apply=True, purge_credentials=True, purge_data=True
                )

            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_repair_rejects_an_agent_id_that_escapes_the_mcp_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, runtime = self._installation(Path(directory))
            runtime["agent_installation_ids"] = ["../../outside"]
            runtime["heartbeat_agent"] = "../../outside"
            (paths.config_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

            with self.assertRaisesRegex(DeviceLifecycleError, "DEVICE_RUNTIME_CONFIG_INVALID"):
                repair_device("linux", paths, apply=True)

    def test_status_handles_a_non_string_agent_id_as_broken_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, runtime = self._installation(Path(directory))
            runtime["agent_installation_ids"] = [{"unexpected": "object"}]
            runtime["heartbeat_agent"] = "codex-linux-test"
            (paths.config_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

            result = device_status("linux", paths)

            self.assertEqual(result["status"], "broken")
            self.assertEqual(result["error"], "DEVICE_RUNTIME_CONFIG_INVALID")

    def test_repair_rejects_a_runtime_for_a_different_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, runtime = self._installation(Path(directory))
            runtime["platform"] = "macos"
            (paths.config_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

            with self.assertRaisesRegex(DeviceLifecycleError, "RUNTIME_PLATFORM_MISMATCH"):
                repair_device("linux", paths, apply=True)

    def test_repair_rejects_unmanaged_python_before_recreating_a_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            paths.service_file.unlink()
            outside = root / "outside-python"
            outside.write_text("do not execute", encoding="utf-8")
            runtime["python_executable"] = str(outside)
            Path(runtime["mcp_config_files"][0]).unlink()
            (paths.config_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

            with self.assertRaisesRegex(DeviceLifecycleError, "PYTHON_EXECUTABLE_NOT_MANAGED"):
                repair_device("linux", paths, apply=True)

            self.assertFalse(paths.service_file.exists())

    def test_repair_validates_all_paths_before_changing_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX file modes are required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            runtime_file = paths.config_dir / "runtime.json"
            runtime_file.chmod(0o644)
            outside = root / "outside.json"
            outside.write_text("keep", encoding="utf-8")
            runtime["mcp_config_files"] = [str(outside)]
            runtime_file.write_text(json.dumps(runtime), encoding="utf-8")

            with self.assertRaisesRegex(DeviceLifecycleError, "MCP_CONFIG_PATHS_INVALID"):
                repair_device("linux", paths, apply=True)

            self.assertEqual(runtime_file.stat().st_mode & 0o777, 0o644)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_uninstall_rejects_a_symlinked_device_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            device_key = Path(runtime["device_key_file"])
            outside = root / "outside-key.pem"
            outside.write_text(device_key.read_text(encoding="utf-8"), encoding="utf-8")
            device_key.unlink()
            device_key.symlink_to(outside)

            with self.assertRaisesRegex(DeviceLifecycleError, "MANAGED_SECRET_SYMLINK_FORBIDDEN"):
                uninstall_device(
                    "linux", paths, apply=True, purge_credentials=True, purge_data=True
                )

            self.assertTrue(outside.is_file())

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_uninstall_validates_data_path_before_removing_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _ = self._installation(root)
            marker = paths.state_dir / "preserved.txt"
            outside = root / "outside-data"
            outside.mkdir()
            paths.state_dir.rename(root / "original-state")
            paths.state_dir.symlink_to(outside, target_is_directory=True)

            remover = mock.Mock()
            with mock.patch(
                "agent_memory_gateway.device_lifecycle._remove_service", remover
            ), self.assertRaisesRegex(DeviceLifecycleError, "UNINSTALL_PATH_FORBIDDEN"):
                uninstall_device(
                    "linux", paths, apply=True, purge_credentials=False, purge_data=True
                )

            remover.assert_not_called()
            self.assertFalse(marker.exists())
            self.assertTrue(outside.is_dir())

    def test_uninstall_rejects_an_unmanaged_service_before_creating_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = self._installation(Path(directory))
            paths.service_file.write_text("unrelated service\n", encoding="utf-8")

            with self.assertRaisesRegex(DeviceLifecycleError, "SERVICE_DEFINITION_NOT_MANAGED"):
                uninstall_device(
                    "linux", paths, apply=True, purge_credentials=False, purge_data=False
                )

            self.assertFalse((paths.config_dir / "backups").exists())

    def test_repair_and_uninstall_reject_modified_mcp_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, runtime = self._installation(Path(directory))
            mcp = Path(runtime["mcp_config_files"][0])
            mcp.write_text('{"unexpected": true}\n', encoding="utf-8")

            with self.assertRaisesRegex(DeviceLifecycleError, "MCP_CONFIG_NOT_MANAGED"):
                repair_device("linux", paths, apply=True)
            with self.assertRaisesRegex(DeviceLifecycleError, "MCP_CONFIG_NOT_MANAGED"):
                uninstall_device(
                    "linux", paths, apply=True, purge_credentials=False, purge_data=False
                )

            self.assertEqual(json.loads(mcp.read_text(encoding="utf-8")), {"unexpected": True})
            self.assertFalse((paths.config_dir / "backups").exists())

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_repair_rejects_a_symlinked_mcp_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            mcp = Path(runtime["mcp_config_files"][0])
            outside = root / "outside.json"
            outside.write_text("keep", encoding="utf-8")
            mcp.unlink()
            mcp.symlink_to(outside)

            with self.assertRaisesRegex(DeviceLifecycleError, "MCP_CONFIG_SYMLINK_FORBIDDEN"):
                repair_device("linux", paths, apply=True)

            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_upgrade_preview_uses_a_new_versioned_runtime_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            package = root / "release"
            (package / "src" / "agent_memory_gateway").mkdir(parents=True)
            (package / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")

            result = upgrade_device(
                "linux", paths, package=package, release_id="v0.2.0", apply=False
            )

            self.assertEqual(result["status"], "planned")
            self.assertIn("runtimes", result["actions"][0]["target_python"])
            self.assertEqual(
                json.loads((paths.config_dir / "runtime.json").read_text(encoding="utf-8"))[
                    "python_executable"
                ],
                runtime["python_executable"],
            )

    def test_upgrade_switches_only_after_staged_install_and_health_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            package = root / "release"
            (package / "src" / "agent_memory_gateway").mkdir(parents=True)
            (package / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
            new_python = paths.data_dir / "runtimes" / "v0.2.0" / "bin" / "python"

            with mock.patch(
                "agent_memory_gateway.device_lifecycle._install_program_runtime",
                return_value=new_python,
            ) as installer, mock.patch(
                "agent_memory_gateway.device_lifecycle._activate_program_runtime"
            ) as activator, mock.patch(
                "agent_memory_gateway.device_lifecycle._wait_for_health", return_value=True
            ):
                result = upgrade_device(
                    "linux", paths, package=package, release_id="v0.2.0", apply=True
                )

            self.assertEqual(result["status"], "upgraded")
            installer.assert_called_once()
            old_runtime, new_runtime = activator.call_args.args[2:4]
            self.assertEqual(old_runtime["python_executable"], runtime["python_executable"])
            self.assertEqual(new_runtime["python_executable"], str(new_python))
            self.assertEqual(new_runtime["previous_python_executable"], runtime["python_executable"])

    def test_upgrade_health_failure_automatically_reactivates_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _ = self._installation(root)
            package = root / "release"
            (package / "src" / "agent_memory_gateway").mkdir(parents=True)
            (package / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
            new_python = paths.data_dir / "runtimes" / "v0.2.0" / "bin" / "python"

            with mock.patch(
                "agent_memory_gateway.device_lifecycle._install_program_runtime",
                return_value=new_python,
            ), mock.patch(
                "agent_memory_gateway.device_lifecycle._activate_program_runtime"
            ) as activator, mock.patch(
                "agent_memory_gateway.device_lifecycle._wait_for_health", return_value=False
            ):
                with self.assertRaisesRegex(
                    DeviceLifecycleError, "PROGRAM_UPGRADE_HEALTH_FAILED_ROLLED_BACK"
                ):
                    upgrade_device(
                        "linux", paths, package=package, release_id="v0.2.0", apply=True
                    )

            self.assertEqual(activator.call_count, 2)
            first = activator.call_args_list[0].args
            second = activator.call_args_list[1].args
            self.assertEqual(first[2], second[3])
            self.assertEqual(first[3], second[2])

    def test_rollback_previews_and_restores_the_previous_managed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, runtime = self._installation(root)
            previous = paths.data_dir / "runtimes" / "v0.1.0" / "bin" / "python"
            previous.parent.mkdir(parents=True)
            previous.write_text("previous runtime", encoding="utf-8")
            runtime["program_release_id"] = "v0.2.0"
            runtime["previous_program_release_id"] = "v0.1.0"
            runtime["previous_python_executable"] = str(previous)
            (paths.config_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

            preview = rollback_device("linux", paths, apply=False)
            with mock.patch(
                "agent_memory_gateway.device_lifecycle._activate_program_runtime"
            ) as activator, mock.patch(
                "agent_memory_gateway.device_lifecycle._wait_for_health", return_value=True
            ):
                applied = rollback_device("linux", paths, apply=True)

            self.assertEqual(preview["status"], "planned")
            self.assertEqual(applied["status"], "rolled_back")
            restored = activator.call_args.args[3]
            self.assertEqual(restored["python_executable"], str(previous))
            self.assertEqual(restored["program_release_id"], "v0.1.0")
            self.assertNotIn("previous_python_executable", restored)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_upgrade_preview_rejects_a_symlinked_versions_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _ = self._installation(root)
            package = root / "release"
            (package / "src" / "agent_memory_gateway").mkdir(parents=True)
            (package / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
            outside = root / "outside-runtimes"
            outside.mkdir()
            (paths.data_dir / "runtimes").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                DeviceLifecycleError, "PROGRAM_RUNTIMES_SYMLINK_FORBIDDEN"
            ):
                upgrade_device(
                    "linux", paths, package=package, release_id="v0.2.0", apply=False
                )


if __name__ == "__main__":
    unittest.main()
