#!/bin/sh
set -eu

ARCHIVE_URL="${MEMORY_DEVICE_ARCHIVE_URL:-}"
EXPECTED_ARCHIVE_SHA256="${MEMORY_DEVICE_ARCHIVE_SHA256:-}"
CHANNEL="${MEMORY_DEVICE_CHANNEL:-stable}"
STABLE_MANIFEST_URL="https://github.com/Buildlee/agent-memory-gateway/releases/latest/download/release-manifest.json"
MAIN_ARCHIVE_URL="https://github.com/Buildlee/agent-memory-gateway/archive/refs/heads/main.zip"
MAX_ARCHIVE_BYTES=268435456

if [ -n "$ARCHIVE_URL" ] && [ -z "$EXPECTED_ARCHIVE_SHA256" ]; then
    echo "自定义 MEMORY_DEVICE_ARCHIVE_URL 必须同时提供 MEMORY_DEVICE_ARCHIVE_SHA256。" >&2
    exit 1
fi

case "$CHANNEL" in
    stable|development) ;;
    *)
        echo "MEMORY_DEVICE_CHANNEL 只接受 stable 或 development。" >&2
        exit 1
        ;;
esac

if [ -n "$EXPECTED_ARCHIVE_SHA256" ]; then
    case "$EXPECTED_ARCHIVE_SHA256" in
        *[!0-9a-fA-F]*)
            echo "MEMORY_DEVICE_ARCHIVE_SHA256 必须是 64 位十六进制摘要。" >&2
            exit 1
            ;;
    esac
    if [ "${#EXPECTED_ARCHIVE_SHA256}" -ne 64 ]; then
        echo "MEMORY_DEVICE_ARCHIVE_SHA256 必须是 64 位十六进制摘要。" >&2
        exit 1
    fi
fi

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "需要 Python 3.10 或更高版本。安装 Python 后重新运行同一条命令。" >&2
    exit 1
fi

"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "需要 Python 3.10 或更高版本。" >&2
    exit 1
}

if [ -z "$ARCHIVE_URL" ]; then
    if [ "$CHANNEL" = "development" ]; then
        ARCHIVE_URL="$MAIN_ARCHIVE_URL"
    else
        if RELEASE_INFO="$($PYTHON - "$STABLE_MANIFEST_URL" <<'PY'
import json
import re
import sys
import urllib.request
from urllib.parse import urlparse

url = sys.argv[1]
parsed = urlparse(url)
if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit("稳定发布清单地址不安全")
request = urllib.request.Request(url, headers={"User-Agent": "agent-memory-gateway-installer/1"})
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        final = urlparse(response.geturl())
        github_redirect = parsed.hostname == "github.com" and (
            final.hostname == "githubusercontent.com" or str(final.hostname or "").endswith(".githubusercontent.com")
        )
        if final.scheme != "https" or not final.hostname or final.username or final.password or final.fragment:
            raise SystemExit("稳定发布清单重定向后的地址不安全")
        if final.query and not github_redirect:
            raise SystemExit("稳定发布清单重定向后的地址包含查询参数")
        payload = response.read(65537)
except Exception as exc:
    raise SystemExit(
        "无法获取稳定发布清单。项目尚未发布稳定版本时请等待 Release；"
        "仅开发测试可设置 MEMORY_DEVICE_CHANNEL=development。"
    ) from exc
if len(payload) > 65536:
    raise SystemExit("稳定发布清单超过 64 KiB")
try:
    manifest = json.loads(payload)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit("稳定发布清单不是有效 JSON") from exc
if set(manifest) != {"version", "release"} or manifest.get("version") != 1 or not isinstance(manifest.get("release"), dict):
    raise SystemExit("稳定发布清单结构无效")
release = manifest["release"]
if set(release) != {"release_id", "archive_url", "sha256"}:
    raise SystemExit("稳定发布清单中的 release 结构无效")
release_id = str(release["release_id"])
archive_url = str(release["archive_url"])
sha256 = str(release["sha256"]).lower()
archive = urlparse(archive_url)
if not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", release_id):
    raise SystemExit("稳定发布清单中的 release_id 无效")
if archive.scheme != "https" or not archive.hostname or archive.username or archive.password or archive.query or archive.fragment:
    raise SystemExit("稳定发布清单中的 archive_url 无效")
if any(character in archive_url for character in "\r\n\t"):
    raise SystemExit("稳定发布清单中的 archive_url 包含控制字符")
if not re.fullmatch(r"[a-f0-9]{64}", sha256):
    raise SystemExit("稳定发布清单中的 sha256 无效")
print("\t".join((release_id, archive_url, sha256)))
PY
)"; then
            :
        elif [ -r /dev/tty ]; then
            printf '%s' "没有可用稳定发布。是否明确改用开发版 main？仅建议开发测试使用 [y/N] " >/dev/tty
            IFS= read -r fallback </dev/tty || fallback=""
            case "$fallback" in
                y|Y|yes|YES|Yes)
                    CHANNEL=development
                    ARCHIVE_URL="$MAIN_ARCHIVE_URL"
                    RELEASE_INFO=""
                    ;;
                *)
                    echo "未确认使用开发通道，安装已停止。" >&2
                    exit 1
                    ;;
            esac
        else
            echo "没有可用稳定发布；非交互环境不会自动使用开发通道。" >&2
            exit 1
        fi
        if [ "$CHANNEL" = "development" ]; then
            :
        else
        RELEASE_ID=$(printf '%s\n' "$RELEASE_INFO" | cut -f 1)
        ARCHIVE_URL=$(printf '%s\n' "$RELEASE_INFO" | cut -f 2)
        EXPECTED_ARCHIVE_SHA256=$(printf '%s\n' "$RELEASE_INFO" | cut -f 3)
        if [ -z "$RELEASE_ID" ] || [ -z "$ARCHIVE_URL" ] || [ -z "$EXPECTED_ARCHIVE_SHA256" ]; then
            echo "稳定发布清单解析结果无效。" >&2
            exit 1
        fi
        echo "已选择稳定发布：$RELEASE_ID"
        fi
    fi
fi

case "$(uname -s)" in
    Darwin) INSTALL_ROOT="$HOME/Library/Application Support/memory-gateway" ;;
    *) INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/memory-gateway" ;;
esac
RUNTIME_ROOT="$INSTALL_ROOT/runtime"
RUNTIME_PYTHON="$RUNTIME_ROOT/bin/python"
MEMORY_DEVICE="$RUNTIME_ROOT/bin/memory-device"
USER_BIN="$HOME/.local/bin"
LAUNCHER_ROOT="$INSTALL_ROOT/bin"
LAUNCHER_PYTHON="$LAUNCHER_ROOT/memory-device-launcher.py"
LAUNCHER_COMMAND="$LAUNCHER_ROOT/memory-device"
case "$(uname -s)" in
    Darwin) RUNTIME_CONFIG="$HOME/Library/Application Support/memory-gateway/runtime.json" ;;
    *) RUNTIME_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/memory-gateway/runtime.json" ;;
esac

if [ -x "$MEMORY_DEVICE" ] && [ -f "$RUNTIME_CONFIG" ] && [ "$#" -eq 0 ]; then
    echo "共享记忆设备工具已经安装：$MEMORY_DEVICE"
    "$MEMORY_DEVICE" status
    echo "需要检查或修复时运行：memory-device doctor / memory-device repair"
    exit 0
fi

if [ -e "$RUNTIME_ROOT" ] && [ ! -x "$RUNTIME_PYTHON" ]; then
    echo "发现不完整的运行环境，拒绝覆盖：$RUNTIME_ROOT" >&2
    exit 1
fi

TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/memory-device-install.XXXXXX")"
trap 'rm -rf "$TEMP_ROOT"' EXIT HUP INT TERM
ARCHIVE_PATH="$TEMP_ROOT/release.zip"
EXTRACT_ROOT="$TEMP_ROOT/release"

"$PYTHON" - "$ARCHIVE_URL" "$ARCHIVE_PATH" "$MAX_ARCHIVE_BYTES" "$EXPECTED_ARCHIVE_SHA256" <<'PY'
import hashlib
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

url, output, maximum, expected = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4].lower()
parsed = urlparse(url)
if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit("源码包地址必须是不带账号、查询参数或片段的 HTTPS 地址")
request = urllib.request.Request(url, headers={"User-Agent": "agent-memory-gateway-installer/1"})
with urllib.request.urlopen(request, timeout=60) as response, output.open("xb") as stream:
    final = urlparse(response.geturl())
    github_redirect = parsed.hostname == "github.com" and (
        final.hostname == "githubusercontent.com" or str(final.hostname or "").endswith(".githubusercontent.com")
    )
    if final.scheme != "https" or not final.hostname or final.username or final.password or final.fragment:
        raise SystemExit("源码包重定向后的地址不安全")
    if final.query and not github_redirect:
        raise SystemExit("源码包重定向后的地址包含查询参数")
    length = response.headers.get("Content-Length")
    if length and int(length) > maximum:
        raise SystemExit("安装包超过 256 MiB，拒绝下载")
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise SystemExit("安装包超过 256 MiB，拒绝下载")
        stream.write(chunk)
        digest.update(chunk)
actual = digest.hexdigest()
if expected and actual != expected:
    raise SystemExit(f"源码包 SHA-256 不匹配：期望 {expected}，实际 {actual}")
print(f"已下载源码包，SHA-256：{actual}")
PY

"$PYTHON" - "$ARCHIVE_PATH" "$EXTRACT_ROOT" <<'PY'
import stat
import sys
import zipfile
from pathlib import Path

archive_path, output = Path(sys.argv[1]), Path(sys.argv[2])
output.mkdir(parents=True)
with zipfile.ZipFile(archive_path) as archive:
    members = archive.infolist()
    if not members or len(members) > 10000:
        raise SystemExit("源码包文件数量无效")
    if sum(member.file_size for member in members) > 512 * 1024 * 1024:
        raise SystemExit("源码包展开后超过 512 MiB，拒绝解压")
    for member in members:
        name = member.filename.replace("\\", "/")
        target = (output / name).resolve()
        if name.startswith("/") or ".." in Path(name).parts or not target.is_relative_to(output.resolve()):
            raise SystemExit("源码包包含不安全路径")
        if stat.S_ISLNK(member.external_attr >> 16):
            raise SystemExit("源码包包含符号链接，拒绝解压")
        file_type = (member.external_attr >> 16) & 0o170000
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise SystemExit("源码包包含特殊文件，拒绝解压")
        if member.file_size > 512 * 1024 * 1024:
            raise SystemExit("源码包中的文件过大")
    archive.extractall(output)

candidates = [
    path.parent
    for path in output.rglob("pyproject.toml")
    if (path.parent / "src" / "agent_memory_gateway").is_dir()
]
if len(candidates) != 1:
    raise SystemExit("源码包没有唯一、完整的项目根目录")
print(candidates[0])
PY

PROJECT_ROOT="$($PYTHON - "$EXTRACT_ROOT" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
candidates = [path.parent for path in root.rglob("pyproject.toml") if (path.parent / "src" / "agent_memory_gateway").is_dir()]
if len(candidates) != 1:
    raise SystemExit(1)
print(candidates[0])
PY
)"

mkdir -p "$INSTALL_ROOT"
if [ ! -x "$RUNTIME_PYTHON" ]; then
    "$PYTHON" -m venv "$RUNTIME_ROOT"
fi
"$RUNTIME_PYTHON" -m pip install --disable-pip-version-check --upgrade "$PROJECT_ROOT[mcp]"
# 主分支快照可能沿用相同版本号；强制刷新本项目代码，但不重复安装第三方依赖。
"$RUNTIME_PYTHON" -m pip install --disable-pip-version-check --force-reinstall --no-deps "$PROJECT_ROOT"

mkdir -p "$USER_BIN"
mkdir -p "$LAUNCHER_ROOT"
cp "$PROJECT_ROOT/scripts/memory-device-launcher.py" "$LAUNCHER_PYTHON"
chmod 0644 "$LAUNCHER_PYTHON"
{
    printf '%s\n' '#!/bin/sh'
    printf 'exec "%s" "%s" "$@"\n' "$RUNTIME_PYTHON" "$LAUNCHER_PYTHON"
} > "$LAUNCHER_COMMAND"
chmod 0755 "$LAUNCHER_COMMAND"
COMMAND_LINK="$USER_BIN/memory-device"
if [ -e "$COMMAND_LINK" ] || [ -L "$COMMAND_LINK" ]; then
    CURRENT_TARGET="$(readlink "$COMMAND_LINK" 2>/dev/null || true)"
    if [ "$CURRENT_TARGET" != "$LAUNCHER_COMMAND" ] && [ "$CURRENT_TARGET" != "$LAUNCHER_PYTHON" ] && [ "$CURRENT_TARGET" != "$MEMORY_DEVICE" ]; then
        echo "命令路径已被其他文件占用，拒绝覆盖：$COMMAND_LINK" >&2
        exit 1
    fi
    if [ "$CURRENT_TARGET" != "$LAUNCHER_COMMAND" ]; then
        rm "$COMMAND_LINK"
        ln -s "$LAUNCHER_COMMAND" "$COMMAND_LINK"
    fi
else
    ln -s "$LAUNCHER_COMMAND" "$COMMAND_LINK"
fi

case ":$PATH:" in
    *":$USER_BIN:"*) ;;
    *) echo "提示：把 $USER_BIN 加入 PATH 后，可直接运行 memory-device。" ;;
esac

if [ -r /dev/tty ]; then
    "$MEMORY_DEVICE" onboard "$@" </dev/tty
else
    echo "程序已安装。请在交互终端运行：$MEMORY_DEVICE onboard" >&2
    exit 2
fi

echo "后续维护：memory-device status、doctor、repair、uninstall"
