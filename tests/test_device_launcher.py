from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "memory_device_launcher", ROOT / "scripts" / "memory-device-launcher.py"
)
assert SPEC is not None and SPEC.loader is not None
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


class DeviceLauncherTests(unittest.TestCase):
    def test_launcher_delegates_to_runtime_config_current_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "runtimes" / "v0.2.0" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("runtime", encoding="utf-8")
            runtime = root / "runtime.json"
            runtime.write_text(json.dumps({"python_executable": str(python)}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"MEMORY_DEVICE_RUNTIME_CONFIG": str(runtime)}), mock.patch(
                "sys.argv", ["memory-device", "status"]
            ), mock.patch.object(LAUNCHER.subprocess, "call", return_value=0) as call:
                result = LAUNCHER.main()

        self.assertEqual(result, 0)
        call.assert_called_once_with(
            [str(python), "-m", "agent_memory_gateway.device_runtime", "status"]
        )

    def test_launcher_rejects_missing_current_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime.json"
            runtime.write_text(
                json.dumps({"python_executable": str(Path(directory) / "missing-python")}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"MEMORY_DEVICE_RUNTIME_CONFIG": str(runtime)}):
                with self.assertRaisesRegex(SystemExit, "找不到当前共享记忆运行环境"):
                    LAUNCHER.main()

    def test_launcher_uses_preserved_bootstrap_after_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_runtime = Path(directory) / "runtime.json"
            with mock.patch.dict(
                os.environ, {"MEMORY_DEVICE_RUNTIME_CONFIG": str(missing_runtime)}
            ), mock.patch("sys.argv", ["memory-device", "onboard"]), mock.patch.object(
                LAUNCHER.subprocess, "call", return_value=0
            ) as call:
                result = LAUNCHER.main()

        self.assertEqual(result, 0)
        call.assert_called_once_with(
            [LAUNCHER.sys.executable, "-m", "agent_memory_gateway.device_runtime", "onboard"]
        )


if __name__ == "__main__":
    unittest.main()
