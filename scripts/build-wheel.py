from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build"
EGG_INFO = ROOT / "src" / "agent_memory_gateway.egg-info"


def _remove_generated_directory(path: Path) -> None:
    resolved = path.resolve()
    allowed = resolved == EGG_INFO.resolve() or (
        resolved.parent == BUILD_ROOT.resolve()
        and resolved.name.startswith(("lib", "bdist.", "temp."))
    )
    if not allowed:
        raise RuntimeError(f"拒绝清理未授权目录：{resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def clean_packaging_cache() -> None:
    _remove_generated_directory(EGG_INFO)
    if BUILD_ROOT.is_dir():
        for path in BUILD_ROOT.iterdir():
            if path.is_dir() and path.name.startswith(("lib", "bdist.", "temp.")):
                _remove_generated_directory(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从干净的 setuptools 缓存构建并检查 wheel")
    parser.add_argument("--outdir", type=Path, default=ROOT / "dist")
    parser.add_argument("--no-isolation", action="store_true")
    args = parser.parse_args(argv)

    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    existing = sorted(outdir.glob("*.whl"))
    if existing:
        raise SystemExit(f"输出目录已有 wheel，拒绝覆盖：{existing[0]}")

    clean_packaging_cache()
    if importlib.util.find_spec("build") is not None:
        command = [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(outdir),
            str(ROOT),
        ]
        if args.no_isolation:
            command.append("--no-isolation")
    else:
        command = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(outdir),
            str(ROOT),
        ]
    subprocess.run(command, cwd=outdir, check=True)

    wheels = sorted(outdir.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"构建后应有且仅有一个 wheel，实际为 {len(wheels)}")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-wheel.py"), str(wheels[0])],
        cwd=ROOT,
        check=True,
    )
    print(wheels[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
