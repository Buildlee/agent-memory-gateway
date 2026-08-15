from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "migrate_metadata.py",
    "verify_gbrain_lifecycle.py",
    "verify_persisted_memory.py",
    "verify_shared_sidecar.py",
    "write_approved_memory.py",
)


def test_direct_maintenance_scripts_prefer_the_checkout_source() -> None:
    for name in SCRIPTS:
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        bootstrap = text.index("prefer_checkout_source(__file__)")
        package_import = text.index("agent_memory_gateway")
        assert bootstrap < package_import, name


def test_source_bootstrap_targets_the_checkout_src_directory() -> None:
    text = (ROOT / "scripts" / "_source_bootstrap.py").read_text(encoding="utf-8")
    assert 'parents[1] / "src"' in text
    assert "sys.path.insert(0, source)" in text


def test_migration_helper_ignores_an_older_package_on_pythonpath(tmp_path: Path) -> None:
    """独立脚本必须优先加载当前 checkout，而不是环境中碰巧安装的旧版本。"""

    fake_package = tmp_path / "agent_memory_gateway"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text("", encoding="utf-8")
    environment = os.environ | {"PYTHONPATH": str(tmp_path)}
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "migrate_metadata.py"), "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
