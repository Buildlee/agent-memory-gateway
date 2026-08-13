from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-wheel.py"
SPEC = importlib.util.spec_from_file_location("check_wheel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECK_WHEEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK_WHEEL)


def _write_wheel(path: Path, extra: dict[str, str] | None = None) -> None:
    files = {
        "agent_memory_gateway/device_runtime.py": "def main(): pass\n",
        "agent_memory_gateway/device_lifecycle.py": "# lifecycle\n",
        "agent_memory_gateway/_schema/migrations/20260812_1_cross_platform_device_types.sql": "SELECT 1;\n",
        "agent_memory_gateway/_schema/migrations/20260812_2_openclaw_agent_type.sql": "SELECT 1;\n",
        "agent_memory_gateway-0.1.0.dist-info/entry_points.txt": (
            "[console_scripts]\n"
            "memory-device = agent_memory_gateway.device_runtime:main\n"
        ),
        "agent_memory_gateway-0.1.0.dist-info/METADATA": (
            "Metadata-Version: 2.4\n"
            "Name: agent-memory-gateway\n"
            "Requires-Python: >=3.10\n"
            "License-Expression: MIT\n"
        ),
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


class WheelSafetyTests(unittest.TestCase):
    def test_clean_wheel_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "clean.whl"
            _write_wheel(wheel)
            self.assertEqual(CHECK_WHEEL.check_wheel(wheel), [])

    def test_temporary_module_and_local_path_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "dirty.whl"
            _write_wheel(
                wheel,
                {
                    "agent_memory_gateway/admin_console_tmp.py": (
                        'SOURCE = "C:/Users/example/project/admin_console.py"\n'
                    )
                },
            )
            errors = CHECK_WHEEL.check_wheel(wheel)
            self.assertTrue(any("临时或缓存文件" in error for error in errors))
            self.assertTrue(any("本机绝对路径" in error for error in errors))

    def test_missing_required_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "missing.whl"
            _write_wheel(wheel)
            with zipfile.ZipFile(wheel) as source:
                kept = {
                    entry.filename: source.read(entry.filename)
                    for entry in source.infolist()
                    if not entry.filename.endswith("device_lifecycle.py")
                }
            with zipfile.ZipFile(wheel, "w") as target:
                for name, content in kept.items():
                    target.writestr(name, content)
            errors = CHECK_WHEEL.check_wheel(wheel)
            self.assertTrue(any("device_lifecycle.py" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
