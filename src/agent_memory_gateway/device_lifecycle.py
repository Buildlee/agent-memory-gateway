"""设备端安装后的状态、诊断、修复和安全卸载。"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .device_key import validate_device_key_file
from .file_credential import read_file_credential
from .memory_app import load_sidecar_environment
from .sidecar_daemon import LocalSidecarProxy, daemon_auth_token
from .windows_credential import delete_generic_credential, read_generic_credential


WINDOWS_TASK_NAME = "MemoryGatewaySidecar"
WINDOWS_TASK_DESCRIPTION = (
    "启动仅回环访问的 Memory Gateway Sidecar；内部 CA 仅在该进程中使用。"
)
IDENTIFIER = re.compile(r"[A-Za-z0-9_.@:-]+")


class DeviceLifecycleError(RuntimeError):
    """设备生命周期操作无法安全完成。"""


@dataclass(frozen=True)
class DeviceCheck:
    name: str
    status: str
    message: str
    repairable: bool = False


def _runtime_path(paths: Any) -> Path:
    return Path(paths.config_dir) / "runtime.json"


def load_installed_runtime(paths: Any) -> dict[str, Any]:
    path = _runtime_path(paths)
    if path.is_symlink():
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_SYMLINK_FORBIDDEN")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeviceLifecycleError("DEVICE_NOT_INSTALLED") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID")
    required = {
        "platform",
        "runtime_config_file",
        "gateway_url",
        "agent_installation_ids",
        "heartbeat_agent",
        "device_id",
        "default_workspace",
        "sidecar_key_file",
        "device_key_file",
        "memory_home",
        "python_executable",
        "port",
    }
    if not required.issubset(value):
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID")
    for key in required - {"agent_installation_ids", "port"}:
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID")
    if IDENTIFIER.fullmatch(value["device_id"]) is None or IDENTIFIER.fullmatch(
        value["default_workspace"]
    ) is None:
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID")
    agents = value.get("agent_installation_ids")
    if not isinstance(agents, list) or not agents:
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID")
    if (
        any(not isinstance(agent, str) or IDENTIFIER.fullmatch(agent) is None for agent in agents)
        or len(set(agents)) != len(agents)
        or value.get("heartbeat_agent") not in agents
    ):
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID")
    if (
        isinstance(value.get("port"), bool)
        or not isinstance(value.get("port"), int)
        or not 1024 <= value["port"] <= 65535
    ):
        raise DeviceLifecycleError("DEVICE_RUNTIME_CONFIG_INVALID")
    return value


def _validate_runtime_scope(
    platform_name: str, paths: Any, runtime: Mapping[str, Any]
) -> None:
    if str(runtime.get("platform") or "") != platform_name:
        raise DeviceLifecycleError("RUNTIME_PLATFORM_MISMATCH")
    expected_runtime = _runtime_path(paths).resolve(strict=False)
    configured_runtime = Path(str(runtime["runtime_config_file"])).resolve(strict=False)
    if configured_runtime != expected_runtime:
        raise DeviceLifecycleError("RUNTIME_CONFIG_PATH_INVALID")
    expected_memory = (Path(paths.state_dir) / "sidecar-v1").resolve(strict=False)
    configured_memory = Path(str(runtime["memory_home"])).resolve(strict=False)
    if configured_memory != expected_memory:
        raise DeviceLifecycleError("MEMORY_HOME_PATH_INVALID")
    _validate_windows_task_name(platform_name, runtime)


def _program_root(platform_name: str, paths: Any) -> Path:
    return Path(paths.config_dir) if platform_name in {"windows", "macos"} else Path(paths.data_dir)


def _managed_python_path(platform_name: str, paths: Any) -> Path:
    executable_name = "python.exe" if platform_name == "windows" else "python"
    executable_parts = (
        ("runtime", "Scripts", executable_name)
        if platform_name == "windows"
        else ("runtime", "bin", executable_name)
    )
    return _program_root(platform_name, paths).joinpath(*executable_parts).resolve(strict=False)


def _managed_versioned_python_path(platform_name: str, paths: Any, release_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9._-]{1,96}", release_id) is None:
        raise DeviceLifecycleError("PROGRAM_RELEASE_ID_INVALID")
    executable_name = "python.exe" if platform_name == "windows" else "python"
    scripts_dir = "Scripts" if platform_name == "windows" else "bin"
    return (
        _program_root(platform_name, paths) / "runtimes" / release_id / scripts_dir / executable_name
    ).resolve(strict=False)


def _is_managed_python_path(platform_name: str, paths: Any, value: str) -> bool:
    configured = Path(value).resolve(strict=False)
    if configured == _managed_python_path(platform_name, paths):
        return True
    versions_path = _program_root(platform_name, paths) / "runtimes"
    if versions_path.is_symlink():
        return False
    versions_root = versions_path.resolve(strict=False)
    try:
        relative = configured.relative_to(versions_root)
    except ValueError:
        return False
    if len(relative.parts) != 3:
        return False
    release_id, scripts_dir, executable_name = relative.parts
    expected_scripts = "Scripts" if platform_name == "windows" else "bin"
    expected_executable = "python.exe" if platform_name == "windows" else "python"
    return (
        re.fullmatch(r"[A-Za-z0-9._-]{1,96}", release_id) is not None
        and scripts_dir == expected_scripts
        and executable_name == expected_executable
    )


def _validate_service_creation_python(
    platform_name: str, paths: Any, runtime: Mapping[str, Any]
) -> None:
    if not _is_managed_python_path(platform_name, paths, str(runtime["python_executable"])):
        raise DeviceLifecycleError("PYTHON_EXECUTABLE_NOT_MANAGED")


def _validate_windows_task_name(platform_name: str, runtime: Mapping[str, Any]) -> None:
    if platform_name == "windows" and str(
        runtime.get("service_task_name") or WINDOWS_TASK_NAME
    ) != WINDOWS_TASK_NAME:
        raise DeviceLifecycleError("WINDOWS_TASK_NAME_INVALID")


def _private_file_ok(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    return os.name == "nt" or stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def _health(runtime: Mapping[str, Any]) -> bool:
    try:
        key_file = Path(str(runtime["sidecar_key_file"]))
        encoded = load_sidecar_environment(
            key_file, require_private_permissions=os.name != "nt"
        )["MEMORY_OUTBOX_KEY"]
        proxy = LocalSidecarProxy(
            f"http://127.0.0.1:{int(runtime['port'])}",
            daemon_auth_token(encoded),
            str(runtime["heartbeat_agent"]),
        )
        return bool(proxy.health())
    except (OSError, RuntimeError, ValueError):
        return False


def _windows_task_xml(task_name: str) -> str | None:
    result = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", task_name, "/XML"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    return str(result.stdout or "")


def _managed_windows_task(task_name: str, runtime: Mapping[str, Any]) -> bool:
    xml = _windows_task_xml(task_name)
    if xml is None:
        return False
    python_path = str(runtime.get("python_executable") or "")
    runtime_path = str(runtime.get("runtime_config_file") or "")
    return (
        "agent_memory_gateway.sidecar_daemon" in xml
        and any(marker in xml for marker in (python_path, _xml(python_path)))
        and any(marker in xml for marker in (runtime_path, _xml(runtime_path)))
    )


def _service_status(platform_name: str, paths: Any, runtime: Mapping[str, Any]) -> str:
    if platform_name == "windows":
        if not _managed_service_definition(platform_name, paths, runtime):
            return "unmanaged"
        task_name = str(runtime.get("service_task_name") or WINDOWS_TASK_NAME)
        xml = _windows_task_xml(task_name)
        if xml is None:
            return "missing"
        return "installed" if _managed_windows_task(task_name, runtime) else "unmanaged"
    if not Path(paths.service_file).is_file():
        return "missing"
    if not _managed_service_definition(platform_name, paths, runtime):
        return "unmanaged"
    if platform_name == "linux":
        result = subprocess.run(
            ["systemctl", "--user", "is-active", Path(paths.service_file).name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return "running" if result.returncode == 0 else "stopped"
    target = f"gui/{os.getuid()}/com.agentmemory.gateway.sidecar"
    result = subprocess.run(
        ["launchctl", "print", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return "running" if result.returncode == 0 else "stopped"


def _mcp_files(runtime: Mapping[str, Any], paths: Any) -> list[Path]:
    root = (
        Path(paths.config_dir) / "mcp"
        if str(runtime.get("platform") or "") == "windows"
        else Path(paths.data_dir) / "mcp"
    )
    if root.is_symlink():
        raise DeviceLifecycleError("MCP_CONFIG_DIRECTORY_SYMLINK_FORBIDDEN")
    expected = [root / f"{agent_id}-mcp.json" for agent_id in runtime["agent_installation_ids"]]
    configured = runtime.get("mcp_config_files")
    if isinstance(configured, list) and configured:
        configured_paths = {Path(str(value)).resolve(strict=False) for value in configured}
        expected_paths = {path.resolve(strict=False) for path in expected}
        if configured_paths != expected_paths:
            raise DeviceLifecycleError("MCP_CONFIG_PATHS_INVALID")
    if any(path.is_symlink() for path in expected):
        raise DeviceLifecycleError("MCP_CONFIG_SYMLINK_FORBIDDEN")
    return expected


def _managed_secret_paths(runtime: Mapping[str, Any], paths: Any) -> tuple[Path, Path, Path | None]:
    secrets_dir = Path(paths.config_dir) / "secrets"
    if secrets_dir.is_symlink():
        raise DeviceLifecycleError("SECRETS_DIRECTORY_SYMLINK_FORBIDDEN")
    sidecar_key = secrets_dir / "sidecar.env"
    device_key = secrets_dir / "device-identity.pem"
    credential_file = None if str(runtime.get("platform") or "") == "windows" else secrets_dir / "device-refresh.json"
    if Path(str(runtime.get("sidecar_key_file") or "")).resolve(strict=False) != sidecar_key.resolve(strict=False):
        raise DeviceLifecycleError("SIDECAR_KEY_PATH_INVALID")
    if Path(str(runtime.get("device_key_file") or device_key)).resolve(strict=False) != device_key.resolve(strict=False):
        raise DeviceLifecycleError("DEVICE_KEY_PATH_INVALID")
    configured_credential = str(runtime.get("credential_file") or "").strip()
    if credential_file is not None and Path(configured_credential).resolve(strict=False) != credential_file.resolve(strict=False):
        raise DeviceLifecycleError("CREDENTIAL_PATH_INVALID")
    if credential_file is None:
        if configured_credential:
            raise DeviceLifecycleError("CREDENTIAL_PATH_INVALID")
        expected_target = f"AgentMemoryGateway/{runtime['device_id']}"
        if str(runtime.get("credential_target") or "") != expected_target:
            raise DeviceLifecycleError("CREDENTIAL_TARGET_INVALID")
    elif str(runtime.get("credential_target") or "").strip():
        raise DeviceLifecycleError("CREDENTIAL_TARGET_INVALID")
    for path in (sidecar_key, device_key, credential_file):
        if path is not None and path.is_symlink():
            raise DeviceLifecycleError(f"MANAGED_SECRET_SYMLINK_FORBIDDEN:{path}")
    return sidecar_key, device_key, credential_file


def device_status(platform_name: str, paths: Any) -> dict[str, Any]:
    runtime_path = _runtime_path(paths)
    try:
        runtime = load_installed_runtime(paths)
        _validate_runtime_scope(platform_name, paths, runtime)
        _managed_secret_paths(runtime, paths)
        _mcp_files(runtime, paths)
    except DeviceLifecycleError as exc:
        return {
            "status": "not_installed" if str(exc) == "DEVICE_NOT_INSTALLED" else "broken",
            "platform": platform_name,
            "runtime_config_file": str(runtime_path),
            "error": str(exc),
        }
    service = _service_status(platform_name, paths, runtime)
    healthy = _health(runtime)
    return {
        "status": "ready" if healthy and service in {"installed", "running"} else "needs_attention",
        "platform": platform_name,
        "device_id": str(runtime["device_id"]),
        "gateway_url": str(runtime["gateway_url"]),
        "default_workspace": str(runtime["default_workspace"]),
        "agent_installation_ids": list(runtime["agent_installation_ids"]),
        "service": service,
        "sidecar_health": "ready" if healthy else "unavailable",
        "runtime_config_file": str(runtime_path),
    }


def diagnose_device(platform_name: str, paths: Any) -> dict[str, Any]:
    checks: list[DeviceCheck] = []
    try:
        runtime = load_installed_runtime(paths)
        _validate_runtime_scope(platform_name, paths, runtime)
        checks.append(DeviceCheck("runtime", "ok", "运行配置有效"))
    except DeviceLifecycleError as exc:
        checks.append(DeviceCheck("runtime", "error", str(exc)))
        return {"status": "failed", "platform": platform_name, "checks": [asdict(item) for item in checks]}

    python_path = Path(str(runtime["python_executable"]))
    checks.append(
        DeviceCheck(
            "python",
            "ok" if python_path.is_file() else "error",
            "运行环境可用" if python_path.is_file() else f"找不到运行环境：{python_path}",
        )
    )
    try:
        sidecar_key, device_key, _ = _managed_secret_paths(runtime, paths)
    except DeviceLifecycleError as exc:
        checks.append(DeviceCheck("managed_paths", "error", str(exc)))
        return {"status": "failed", "platform": platform_name, "checks": [asdict(item) for item in checks]}
    permission_paths = [_runtime_path(paths), sidecar_key, device_key]
    configured_credential = str(runtime.get("credential_file") or "").strip()
    if configured_credential:
        permission_paths.append(Path(configured_credential))
    permission_errors = [str(path) for path in permission_paths if not _private_file_ok(path)]
    checks.append(
        DeviceCheck(
            "private_files",
            "error" if permission_errors else "ok",
            "权限过宽或文件缺失：" + ", ".join(permission_errors)
            if permission_errors
            else "本机敏感文件权限有效",
            repairable=bool(permission_errors) and os.name != "nt",
        )
    )
    try:
        load_sidecar_environment(sidecar_key, require_private_permissions=os.name != "nt")
        checks.append(DeviceCheck("sidecar_key", "ok", "Sidecar key 有效"))
    except (OSError, RuntimeError):
        checks.append(
            DeviceCheck(
                "sidecar_key",
                "error",
                "Sidecar key 缺失、格式错误或权限过宽",
                repairable=sidecar_key.is_file() and os.name != "nt",
            )
        )

    try:
        validate_device_key_file(device_key)
        checks.append(DeviceCheck("device_key", "ok", "设备私钥有效"))
    except (OSError, ValueError):
        checks.append(DeviceCheck("device_key", "error", "设备私钥缺失或无效"))

    credential_file = str(runtime.get("credential_file") or "").strip()
    credential_target = str(runtime.get("credential_target") or "").strip()
    try:
        credential = (
            read_file_credential(Path(credential_file))
            if credential_file
            else read_generic_credential(credential_target)
        )
        checks.append(
            DeviceCheck(
                "credential",
                "ok" if credential is not None else "error",
                "刷新凭据可用" if credential is not None else "刷新凭据不存在",
            )
        )
    except (OSError, RuntimeError):
        checks.append(DeviceCheck("credential", "error", "刷新凭据不可读取"))

    try:
        managed_mcp_files = _mcp_files(runtime, paths)
        missing_mcp = [str(path) for path in managed_mcp_files if not path.is_file()]
        invalid_mcp = [str(path) for path in _invalid_mcp_files(runtime, paths)]
    except DeviceLifecycleError as exc:
        checks.append(DeviceCheck("mcp_config", "error", str(exc)))
        return {
            "status": "failed",
            "platform": platform_name,
            "checks": [asdict(item) for item in checks],
        }
    checks.append(
        DeviceCheck(
            "mcp_config",
            "error" if missing_mcp else "ok",
            "缺少 MCP 配置：" + ", ".join(missing_mcp) if missing_mcp else "MCP 配置齐全",
            repairable=bool(missing_mcp),
        )
    )
    checks.append(
        DeviceCheck(
            "mcp_content",
            "error" if invalid_mcp else "ok",
            "MCP 配置与安装记录不一致：" + ", ".join(invalid_mcp)
            if invalid_mcp
            else "MCP 配置内容有效",
        )
    )
    insecure_mcp = [
        str(path) for path in managed_mcp_files if path.is_file() and not _private_file_ok(path)
    ]
    checks.append(
        DeviceCheck(
            "mcp_permissions",
            "error" if insecure_mcp else "ok",
            "MCP 配置权限过宽：" + ", ".join(insecure_mcp)
            if insecure_mcp
            else "MCP 配置权限有效",
            repairable=bool(insecure_mcp) and os.name != "nt",
        )
    )
    service = _service_status(platform_name, paths, runtime)
    checks.append(
        DeviceCheck(
            "service",
            "ok" if service in {"installed", "running"} else "error",
            f"后台服务状态：{service}",
            repairable=service == "stopped",
        )
    )
    healthy = _health(runtime)
    checks.append(
        DeviceCheck(
            "sidecar",
            "ok" if healthy else "error",
            "Sidecar 健康检查通过" if healthy else "Sidecar 健康检查失败",
            repairable=service in {"installed", "running", "stopped"},
        )
    )
    return {
        "status": "ok" if all(item.status == "ok" for item in checks) else "needs_attention",
        "platform": platform_name,
        "checks": [asdict(item) for item in checks],
    }


def _agent_type(runtime: Mapping[str, Any], agent_id: str) -> str:
    agents = runtime.get("agents")
    if isinstance(agents, list):
        for value in agents:
            if isinstance(value, dict) and str(value.get("installation_id") or "") == agent_id:
                return str(value.get("type") or "other")
    return "other"


def _render_mcp(runtime: Mapping[str, Any], agent_id: str) -> str:
    from .device_runtime import render_mcp_config

    value = render_mcp_config(
        python_executable=str(runtime["python_executable"]),
        agent_installation_id=agent_id,
        agent_type=_agent_type(runtime, agent_id),
        workspace_id=str(runtime["default_workspace"]),
        sidecar_key_file=Path(str(runtime["sidecar_key_file"])),
        port=int(runtime["port"]),
    )
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _atomic_replace_text(path: Path, text: str, *, private: bool) -> None:
    if path.is_symlink() or not path.is_file():
        raise DeviceLifecycleError(f"MANAGED_FILE_INVALID:{path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if private and os.name != "nt":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def _runtime_text(runtime: Mapping[str, Any]) -> str:
    return json.dumps(dict(runtime), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _package_argument(package: Path) -> str:
    resolved = package.resolve(strict=True)
    if resolved.is_dir():
        if not (resolved / "pyproject.toml").is_file() or not (
            resolved / "src" / "agent_memory_gateway"
        ).is_dir():
            raise DeviceLifecycleError("PROGRAM_PACKAGE_INVALID")
        return f"{resolved}[mcp]"
    if resolved.is_file() and resolved.suffix == ".whl":
        return f"{resolved}[mcp]"
    raise DeviceLifecycleError("PROGRAM_PACKAGE_INVALID")


def _install_program_runtime(
    platform_name: str,
    paths: Any,
    package: Path,
    release_id: str,
    bootstrap_python: Path,
) -> Path:
    package_argument = _package_argument(package)
    target_python = _managed_versioned_python_path(platform_name, paths, release_id)
    runtime_root = target_python.parent.parent
    versions_path = _program_root(platform_name, paths) / "runtimes"
    if versions_path.is_symlink():
        raise DeviceLifecycleError("PROGRAM_RUNTIMES_SYMLINK_FORBIDDEN")
    versions_root = versions_path.resolve(strict=False)
    versions_root.mkdir(parents=True, exist_ok=True)
    if runtime_root.is_symlink() or runtime_root.exists():
        raise DeviceLifecycleError(f"PROGRAM_RELEASE_EXISTS:{runtime_root}")
    staging_root = Path(tempfile.mkdtemp(prefix=f".{release_id}.staging-", dir=versions_root))
    try:
        staging_python = staging_root / target_python.relative_to(runtime_root)
        subprocess.run([str(bootstrap_python), "-m", "venv", str(staging_root)], check=True)
        if not staging_python.is_file():
            raise DeviceLifecycleError("PROGRAM_RUNTIME_CREATE_FAILED")
        subprocess.run(
            [
                str(staging_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--upgrade",
                package_argument,
            ],
            check=True,
        )
        subprocess.run(
            [
                str(staging_python),
                "-c",
                "import agent_memory_gateway.device_runtime; import agent_memory_gateway.sidecar_daemon",
            ],
            check=True,
        )
        os.replace(staging_root, runtime_root)
    except Exception:
        if staging_root.exists() and not staging_root.is_symlink():
            shutil.rmtree(staging_root)
        raise
    if not target_python.is_file():
        raise DeviceLifecycleError("PROGRAM_RUNTIME_ACTIVATION_FAILED")
    return target_python


def _replace_managed_runtime_files(paths: Any, runtime: Mapping[str, Any]) -> None:
    runtime_file = _runtime_path(paths)
    mcp_files = _mcp_files(runtime, paths)
    _atomic_replace_text(runtime_file, _runtime_text(runtime), private=True)
    for agent_id, path in zip(runtime["agent_installation_ids"], mcp_files, strict=True):
        _atomic_replace_text(path, _render_mcp(runtime, str(agent_id)), private=True)


def _activate_program_runtime(
    platform_name: str,
    paths: Any,
    old_runtime: Mapping[str, Any],
    new_runtime: Mapping[str, Any],
) -> None:
    from .device_runtime import write_service_definition

    if not _managed_service_definition(platform_name, paths, old_runtime):
        raise DeviceLifecycleError("SERVICE_DEFINITION_NOT_MANAGED")
    _remove_service(platform_name, paths, old_runtime)
    Path(paths.service_file).unlink(missing_ok=True)
    try:
        _replace_managed_runtime_files(paths, new_runtime)
        write_service_definition(new_runtime, platform_name, Path(paths.service_file), enable=True)
    except Exception:
        try:
            if _managed_service_definition(platform_name, paths, new_runtime):
                _remove_service(platform_name, paths, new_runtime)
        except Exception:
            pass
        Path(paths.service_file).unlink(missing_ok=True)
        _replace_managed_runtime_files(paths, old_runtime)
        write_service_definition(old_runtime, platform_name, Path(paths.service_file), enable=True)
        raise


def _wait_for_health(runtime: Mapping[str, Any], *, attempts: int = 20) -> bool:
    for _ in range(attempts):
        if _health(runtime):
            return True
        time.sleep(0.5)
    return False


def upgrade_device(
    platform_name: str,
    paths: Any,
    *,
    package: Path,
    release_id: str,
    apply: bool,
) -> dict[str, Any]:
    runtime = load_installed_runtime(paths)
    _validate_runtime_scope(platform_name, paths, runtime)
    _managed_secret_paths(runtime, paths)
    if _invalid_mcp_files(runtime, paths):
        raise DeviceLifecycleError("MCP_CONFIG_NOT_MANAGED")
    if not _managed_service_definition(platform_name, paths, runtime):
        raise DeviceLifecycleError("SERVICE_DEFINITION_NOT_MANAGED")
    if re.fullmatch(r"[A-Za-z0-9._-]{1,96}", release_id) is None:
        raise DeviceLifecycleError("PROGRAM_RELEASE_ID_INVALID")
    package_path = package.resolve(strict=True)
    target_python = _managed_versioned_python_path(platform_name, paths, release_id)
    versions_path = _program_root(platform_name, paths) / "runtimes"
    if versions_path.is_symlink():
        raise DeviceLifecycleError("PROGRAM_RUNTIMES_SYMLINK_FORBIDDEN")
    if target_python.parent.parent.exists():
        raise DeviceLifecycleError(f"PROGRAM_RELEASE_EXISTS:{target_python.parent.parent}")
    action = {
        "action": "activate_program_release",
        "release_id": release_id,
        "package": str(package_path),
        "target_python": str(target_python),
    }
    if not apply:
        _package_argument(package_path)
        return {"status": "planned", "actions": [action]}

    old_python = Path(str(runtime["python_executable"])).resolve(strict=False)
    if not old_python.is_file() or not _is_managed_python_path(platform_name, paths, str(old_python)):
        raise DeviceLifecycleError("PYTHON_EXECUTABLE_NOT_MANAGED")
    new_python = _install_program_runtime(platform_name, paths, package_path, release_id, old_python)
    new_runtime = dict(runtime)
    new_runtime["python_executable"] = str(new_python)
    new_runtime["program_release_id"] = release_id
    new_runtime["previous_python_executable"] = str(old_python)
    previous_release = str(runtime.get("program_release_id") or "").strip()
    if previous_release:
        new_runtime["previous_program_release_id"] = previous_release
    else:
        new_runtime.pop("previous_program_release_id", None)
    _activate_program_runtime(platform_name, paths, runtime, new_runtime)
    if not _wait_for_health(new_runtime):
        _activate_program_runtime(platform_name, paths, new_runtime, runtime)
        raise DeviceLifecycleError("PROGRAM_UPGRADE_HEALTH_FAILED_ROLLED_BACK")
    return {"status": "upgraded", "release_id": release_id, "python_executable": str(new_python)}


def rollback_device(platform_name: str, paths: Any, *, apply: bool) -> dict[str, Any]:
    runtime = load_installed_runtime(paths)
    _validate_runtime_scope(platform_name, paths, runtime)
    _managed_secret_paths(runtime, paths)
    if _invalid_mcp_files(runtime, paths):
        raise DeviceLifecycleError("MCP_CONFIG_NOT_MANAGED")
    previous_value = str(runtime.get("previous_python_executable") or "").strip()
    if not previous_value:
        raise DeviceLifecycleError("PROGRAM_ROLLBACK_NOT_AVAILABLE")
    previous_python = Path(previous_value).resolve(strict=False)
    if not previous_python.is_file() or not _is_managed_python_path(
        platform_name, paths, str(previous_python)
    ):
        raise DeviceLifecycleError("PROGRAM_ROLLBACK_TARGET_INVALID")
    action = {"action": "rollback_program_release", "target_python": str(previous_python)}
    if not apply:
        return {"status": "planned", "actions": [action]}

    restored = dict(runtime)
    restored["python_executable"] = str(previous_python)
    previous_release = str(runtime.get("previous_program_release_id") or "").strip()
    if previous_release:
        restored["program_release_id"] = previous_release
    else:
        restored.pop("program_release_id", None)
    restored.pop("previous_python_executable", None)
    restored.pop("previous_program_release_id", None)
    _activate_program_runtime(platform_name, paths, runtime, restored)
    if not _wait_for_health(restored):
        _activate_program_runtime(platform_name, paths, restored, runtime)
        raise DeviceLifecycleError("PROGRAM_ROLLBACK_HEALTH_FAILED_RESTORED_CURRENT")
    return {
        "status": "rolled_back",
        "release_id": restored.get("program_release_id"),
        "python_executable": str(previous_python),
    }


def _invalid_mcp_files(runtime: Mapping[str, Any], paths: Any) -> list[Path]:
    invalid: list[Path] = []
    for agent_id, path in zip(
        runtime["agent_installation_ids"], _mcp_files(runtime, paths), strict=True
    ):
        if not path.is_file():
            continue
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            expected = json.loads(_render_mcp(runtime, str(agent_id)))
        except (OSError, UnicodeError, ValueError):
            invalid.append(path)
            continue
        if current != expected:
            invalid.append(path)
    return invalid


def repair_device(platform_name: str, paths: Any, *, apply: bool) -> dict[str, Any]:
    runtime = load_installed_runtime(paths)
    _validate_runtime_scope(platform_name, paths, runtime)
    sidecar_key, _, credential_path = _managed_secret_paths(runtime, paths)
    mcp_files = _mcp_files(runtime, paths)
    invalid_mcp = _invalid_mcp_files(runtime, paths)
    if invalid_mcp:
        raise DeviceLifecycleError(
            "MCP_CONFIG_NOT_MANAGED:" + ",".join(str(path) for path in invalid_mcp)
        )
    if not _managed_service_definition(platform_name, paths, runtime):
        raise DeviceLifecycleError("SERVICE_DEFINITION_NOT_MANAGED")
    service = _service_status(platform_name, paths, runtime)
    if service == "unmanaged":
        raise DeviceLifecycleError("SERVICE_NOT_MANAGED")
    if service in {"missing", "stopped"} or (
        platform_name == "windows" and service == "installed" and not _health(runtime)
    ):
        _validate_service_creation_python(platform_name, paths, runtime)

    actions: list[dict[str, str]] = []
    private_files = [_runtime_path(paths), sidecar_key, Path(str(runtime["device_key_file"])), *mcp_files]
    if credential_path is not None:
        private_files.append(credential_path)
    for path in private_files:
        if path.is_symlink():
            raise DeviceLifecycleError(f"PRIVATE_FILE_SYMLINK_FORBIDDEN:{path}")
        if path.is_file() and os.name != "nt" and not _private_file_ok(path):
            actions.append({"action": "chmod_0600", "target": str(path)})
            if apply:
                path.chmod(0o600)

    for agent_id, path in zip(runtime["agent_installation_ids"], mcp_files, strict=True):
        if path.exists():
            continue
        actions.append({"action": "create_mcp_config", "target": str(path)})
        if apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render_mcp(runtime, str(agent_id)), encoding="utf-8", newline="\n")
            if os.name != "nt":
                path.chmod(0o600)

    if service == "missing":
        service_file = Path(paths.service_file)
        if service_file.is_file():
            actions.append({"action": "enable_service", "target": str(service_file)})
            if apply:
                from .device_runtime import enable_service_definition

                enable_service_definition(platform_name, service_file)
        else:
            actions.append({"action": "create_service_definition", "target": str(service_file)})
            if apply:
                from .device_runtime import write_service_definition

                write_service_definition(runtime, platform_name, service_file, enable=True)
    elif service == "stopped":
        actions.append({"action": "restart_service", "target": str(paths.service_file)})
        if apply:
            from .device_runtime import enable_service_definition

            enable_service_definition(platform_name, Path(paths.service_file))
    elif platform_name == "windows" and service == "installed" and not _health(runtime):
        task_name = str(runtime.get("service_task_name") or WINDOWS_TASK_NAME)
        actions.append({"action": "start_service", "target": task_name})
        if apply:
            subprocess.run(["schtasks.exe", "/Run", "/TN", task_name], check=True)

    if apply and actions:
        time.sleep(0.5)
    return {
        "status": "completed" if apply else "planned",
        "actions": actions,
        "diagnosis": diagnose_device(platform_name, paths) if apply else None,
    }


def _managed_service_definition(platform_name: str, paths: Any, runtime: Mapping[str, Any]) -> bool:
    service_file = Path(paths.service_file)
    if service_file.is_symlink():
        return False
    if not service_file.is_file():
        return True
    from .device_runtime import (
        render_launchd_plist,
        render_systemd_user_unit,
        render_windows_service_manifest,
    )

    expected = (
        render_systemd_user_unit(runtime)
        if platform_name == "linux"
        else render_launchd_plist(runtime)
        if platform_name == "macos"
        else render_windows_service_manifest(runtime)
        if platform_name == "windows"
        else ""
    )
    try:
        return service_file.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeError):
        return False


def _backup_non_sensitive(runtime: Mapping[str, Any], paths: Any) -> Path:
    backup_dir = Path(paths.config_dir) / "backups"
    if backup_dir.is_symlink():
        raise DeviceLifecycleError("BACKUP_DIRECTORY_SYMLINK_FORBIDDEN")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("device-uninstall-%Y%m%d-%H%M%S", time.localtime())
    destination = backup_dir / f"{stamp}-{time.time_ns() % 1_000_000_000:09d}.zip"
    candidates = [
        ("runtime.json", _runtime_path(paths)),
        *((f"mcp/{path.name}", path) for path in _mcp_files(runtime, paths)),
    ]
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_name, path in candidates:
            if path.is_file():
                archive.write(path, archive_name)
    return destination


def _remove_service(platform_name: str, paths: Any, runtime: Mapping[str, Any]) -> None:
    if platform_name == "windows":
        task_name = str(runtime.get("service_task_name") or WINDOWS_TASK_NAME)
        xml = _windows_task_xml(task_name)
        if xml is None:
            return
        if not _managed_windows_task(task_name, runtime):
            raise DeviceLifecycleError("WINDOWS_TASK_NOT_MANAGED")
        subprocess.run(
            ["schtasks.exe", "/End", "/TN", task_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(["schtasks.exe", "/Delete", "/F", "/TN", task_name], check=True)
        return
    service_file = Path(paths.service_file)
    if not _managed_service_definition(platform_name, paths, runtime):
        raise DeviceLifecycleError("SERVICE_DEFINITION_NOT_MANAGED")
    if platform_name == "linux":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", service_file.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        service_file.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        return
    target = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", target, str(service_file)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    service_file.unlink(missing_ok=True)


def _validated_remove_tree(target: Path, allowed_parent: Path) -> Path:
    if target.is_symlink():
        raise DeviceLifecycleError(f"UNINSTALL_PATH_FORBIDDEN:{target}")
    try:
        resolved = target.resolve(strict=False)
        parent = allowed_parent.resolve(strict=False)
    except OSError as exc:
        raise DeviceLifecycleError("UNINSTALL_PATH_INVALID") from exc
    if resolved == parent or not resolved.is_relative_to(parent):
        raise DeviceLifecycleError(f"UNINSTALL_PATH_FORBIDDEN:{resolved}")
    return resolved


def _safe_remove_tree(target: Path, allowed_parent: Path) -> None:
    resolved = _validated_remove_tree(target, allowed_parent)
    if resolved.exists():
        shutil.rmtree(resolved)


def uninstall_device(
    platform_name: str,
    paths: Any,
    *,
    apply: bool,
    purge_credentials: bool,
    purge_data: bool,
) -> dict[str, Any]:
    runtime = load_installed_runtime(paths)
    _validate_runtime_scope(platform_name, paths, runtime)
    sidecar_key, device_key, credential_path = _managed_secret_paths(runtime, paths)
    invalid_mcp = _invalid_mcp_files(runtime, paths)
    if invalid_mcp:
        raise DeviceLifecycleError(
            "MCP_CONFIG_NOT_MANAGED:" + ",".join(str(path) for path in invalid_mcp)
        )
    if platform_name == "windows":
        task_name = str(runtime.get("service_task_name") or WINDOWS_TASK_NAME)
        if _windows_task_xml(task_name) is not None and not _managed_windows_task(task_name, runtime):
            raise DeviceLifecycleError("WINDOWS_TASK_NOT_MANAGED")
    elif not _managed_service_definition(platform_name, paths, runtime):
        raise DeviceLifecycleError("SERVICE_DEFINITION_NOT_MANAGED")
    if purge_data:
        _validated_remove_tree(Path(paths.state_dir), Path(paths.state_dir).parent)
    service_target = (
        str(runtime.get("service_task_name") or WINDOWS_TASK_NAME)
        if platform_name == "windows"
        else str(paths.service_file)
    )
    actions = [
        {"action": "remove_service", "target": service_target},
        *({"action": "remove_mcp_config", "target": str(path)} for path in _mcp_files(runtime, paths)),
        {"action": "remove_runtime_config", "target": str(_runtime_path(paths))},
    ]
    if purge_credentials:
        actions.extend(
            {"action": "remove_credential", "target": str(value)}
            for value in (
                credential_path or runtime.get("credential_target"),
                device_key,
                sidecar_key,
            )
            if value
        )
    if purge_data:
        actions.append({"action": "remove_local_data", "target": str(paths.state_dir)})
    if not apply:
        return {
            "status": "planned",
            "actions": actions,
            "preserved": {
                "credentials": not purge_credentials,
                "local_data": not purge_data,
            },
        }

    backup = _backup_non_sensitive(runtime, paths)
    _remove_service(platform_name, paths, runtime)
    Path(paths.service_file).unlink(missing_ok=True)
    for path in _mcp_files(runtime, paths):
        path.unlink(missing_ok=True)
    if purge_credentials:
        credential_file = str(credential_path or "").strip()
        credential_target = str(runtime.get("credential_target") or "").strip()
        if credential_file:
            Path(credential_file).unlink(missing_ok=True)
        elif credential_target:
            delete_generic_credential(credential_target)
        device_key.unlink(missing_ok=True)
        sidecar_key.unlink(missing_ok=True)
    if purge_data:
        _safe_remove_tree(Path(paths.state_dir), Path(paths.state_dir).parent)
    _runtime_path(paths).unlink(missing_ok=True)
    return {
        "status": "uninstalled",
        "backup": str(backup),
        "preserved": {
            "credentials": not purge_credentials,
            "local_data": not purge_data,
            "program": True,
        },
    }
