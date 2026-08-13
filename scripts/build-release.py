"""构建可复现的 GitHub Release 资产与稳定安装清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
TAG_PATTERN = re.compile(r"v(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?)\Z")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        check=True,
    )
    values = [Path(os.fsdecode(value)) for value in result.stdout.split(b"\0") if value]
    if not values:
        raise RuntimeError("仓库没有可发布的已跟踪文件")
    for value in values:
        if value.is_absolute() or ".." in value.parts or not (ROOT / value).is_file():
            raise RuntimeError(f"发布文件路径无效：{value}")
    return sorted(values, key=lambda value: value.as_posix())


def source_timestamp() -> tuple[int, int, int, int, int, int]:
    epoch = int(
        subprocess.run(
            ["git", "show", "-s", "--format=%ct", "HEAD"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()
    )
    return time.gmtime(max(epoch, 315532800))[:6]


def build_source_archive(output: Path, prefix: str) -> None:
    timestamp = source_timestamp()
    files = tracked_files()
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            source = ROOT / relative
            name = PurePosixPath(prefix, relative.as_posix()).as_posix()
            info = zipfile.ZipInfo(name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if source.suffix in {".sh"} else 0o100644) << 16
            archive.writestr(info, source.read_bytes())


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    if match is None:
        raise RuntimeError("pyproject.toml 缺少项目版本")
    return match.group(1)


def refuse_existing(paths: list[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("拒绝覆盖已有发布资产：" + ", ".join(existing))


def main() -> None:
    parser = argparse.ArgumentParser(description="构建固定源码包、安装脚本、发布清单和 SHA-256 清单")
    parser.add_argument("--tag", required=True, help="语义版本标签，例如 v0.1.0")
    parser.add_argument("--repository", default="Buildlee/agent-memory-gateway", help="GitHub owner/repo")
    parser.add_argument("--outdir", type=Path, default=ROOT / "dist")
    parser.add_argument("--allow-dirty", action="store_true", help="仅用于本机验证未提交改动")
    args = parser.parse_args()

    tag_match = TAG_PATTERN.fullmatch(args.tag)
    if tag_match is None:
        parser.error("--tag 必须是 v 开头的语义版本，例如 v0.1.0")
    version = tag_match.group("version")
    if version != project_version():
        parser.error(f"标签版本 {version} 与 pyproject.toml 版本 {project_version()} 不一致")
    if REPOSITORY_PATTERN.fullmatch(args.repository) is None:
        parser.error("--repository 必须是 owner/repo")
    if not args.allow_dirty:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        if status:
            parser.error("发布构建要求干净工作区；本机验证可显式传入 --allow-dirty")

    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    existing_entries = sorted(outdir.iterdir(), key=lambda path: path.name)
    if (
        len(existing_entries) != 1
        or not existing_entries[0].is_file()
        or not re.fullmatch(r"agent_memory_gateway-.+\.whl", existing_entries[0].name)
    ):
        parser.error("发布输出目录必须预先包含且仅包含一个已校验的 agent_memory_gateway wheel")
    archive = outdir / f"agent-memory-gateway-{version}.zip"
    manifest = outdir / "release-manifest.json"
    checksums = outdir / "SHA256SUMS"
    installer_ps1 = outdir / "memory-device-install.ps1"
    installer_sh = outdir / "memory-device-install.sh"
    refuse_existing([archive, manifest, checksums, installer_ps1, installer_sh])

    build_source_archive(archive, f"agent-memory-gateway-{version}")
    shutil.copyfile(ROOT / "scripts" / installer_ps1.name, installer_ps1)
    shutil.copyfile(ROOT / "scripts" / installer_sh.name, installer_sh)
    release = {
        "version": 1,
        "release": {
            "release_id": args.tag,
            "archive_url": (
                f"https://github.com/{args.repository}/releases/download/{args.tag}/{archive.name}"
            ),
            "sha256": sha256(archive),
        },
    }
    manifest.write_text(
        json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    assets = sorted(
        [path for path in outdir.iterdir() if path.is_file() and path.name != checksums.name],
        key=lambda path: path.name,
    )
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in assets),
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"status": "built", "release": release["release"], "assets": [str(path) for path in assets]}))


if __name__ == "__main__":
    main()
