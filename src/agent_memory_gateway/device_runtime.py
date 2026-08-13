"""Windows、Linux 和 macOS 共用的设备运行配置与自启动描述。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .device_key import validate_device_key_file
from .device_pair import DevicePairAgent, pair_device
from .file_credential import read_file_credential
from .sidecar_daemon import LocalSidecarProxy, daemon_auth_token
from .sidecar_key import generate_sidecar_key_file
from .windows_credential import read_generic_credential


class DeviceRuntimeError(RuntimeError):
    """跨平台设备运行配置错误。"""


IDENTIFIER = re.compile(r"[A-Za-z0-9_.@:-]{1,256}\Z")
ALLOWED_PLATFORMS = frozenset({"windows", "linux", "macos"})
SENSITIVE_FIELD_MARKERS = (
    "secret",
    "credential",
    "token",
    "pairing",
    "password",
    "private",
    "refresh",
    "dsn",
    "connection",
)
AGENT_DISPLAY_NAMES = {
    "codex": "Codex",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "other": "Other Agent",
}


@dataclass(frozen=True)
class PlatformPaths:
    config_dir: Path
    state_dir: Path
    data_dir: Path
    service_file: Path


def current_platform() -> str:
    name = platform.system().lower()
    if name == "windows":
        return "windows"
    if name == "darwin":
        return "macos"
    if name == "linux":
        return "linux"
    raise DeviceRuntimeError("PLATFORM_UNSUPPORTED")


def platform_paths(platform_name: str | None = None, environment: Mapping[str, str] | None = None) -> PlatformPaths:
    selected = platform_name or current_platform()
    if selected not in ALLOWED_PLATFORMS:
        raise DeviceRuntimeError("PLATFORM_UNSUPPORTED")
    env = dict(os.environ if environment is None else environment)
    home_value = (
        env.get("USERPROFILE")
        if selected == "windows"
        else env.get("HOME")
    ) or str(Path.home())
    home = Path(home_value)
    if selected == "windows":
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "memory-gateway"
        return PlatformPaths(base, base / "state", base, base / "service.json")
    if selected == "macos":
        config = home / "Library" / "Application Support" / "memory-gateway"
        state = home / "Library" / "Application Support" / "memory-gateway" / "state"
        return PlatformPaths(
            config,
            state,
            config,
            home / "Library" / "LaunchAgents" / "com.agentmemory.gateway.sidecar.plist",
        )
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "memory-gateway"
    state = Path(env.get("XDG_STATE_HOME") or home / ".local" / "state") / "memory-gateway"
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "memory-gateway"
    return PlatformPaths(
        config,
        state,
        data,
        config.parent / "systemd" / "user" / "memory-gateway-sidecar.service",
    )


def validate_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise DeviceRuntimeError("INSTALL_PROFILE_OBJECT_REQUIRED")

    def reject_sensitive(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if any(marker in str(key).lower() for marker in SENSITIVE_FIELD_MARKERS):
                    raise DeviceRuntimeError(f"INSTALL_PROFILE_SENSITIVE_FIELD:{path}.{key}")
                reject_sensitive(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                reject_sensitive(child, f"{path}[{index}]")

    reject_sensitive(profile, "profile")
    allowed = {"version", "gateway_url", "default_workspace", "device_id_prefix", "agents", "release"}
    if not set(profile).issubset(allowed) or profile.get("version", 1) != 1:
        raise DeviceRuntimeError("INSTALL_PROFILE_INVALID")
    gateway_url = str(profile.get("gateway_url") or "")
    gateway = urlsplit(gateway_url)
    try:
        gateway_port = gateway.port
    except ValueError as exc:
        raise DeviceRuntimeError("INSTALL_PROFILE_GATEWAY_URL_INVALID") from exc
    if (
        gateway.scheme != "https"
        or not gateway.hostname
        or gateway.username
        or gateway.password
        or gateway.path not in {"", "/"}
        or gateway.query
        or gateway.fragment
        or (gateway_port is not None and not 1 <= gateway_port <= 65535)
    ):
        raise DeviceRuntimeError("INSTALL_PROFILE_GATEWAY_URL_INVALID")
    workspace = str(profile.get("default_workspace") or "")
    if IDENTIFIER.fullmatch(workspace) is None:
        raise DeviceRuntimeError("INSTALL_PROFILE_WORKSPACE_INVALID")
    prefix = str(profile.get("device_id_prefix") or "")
    if prefix and IDENTIFIER.fullmatch(prefix) is None:
        raise DeviceRuntimeError("INSTALL_PROFILE_DEVICE_PREFIX_INVALID")
    agents = profile.get("agents")
    if not isinstance(agents, list) or not 1 <= len(agents) <= 16:
        raise DeviceRuntimeError("INSTALL_PROFILE_AGENTS_INVALID")
    for agent in agents:
        if not isinstance(agent, dict) or not set(agent).issubset(
            {"type", "display_name", "installation_id_template"}
        ):
            raise DeviceRuntimeError("INSTALL_PROFILE_AGENTS_INVALID")
        if agent.get("type") not in {"codex", "hermes", "openclaw", "other"} or not str(agent.get("display_name") or "").strip():
            raise DeviceRuntimeError("INSTALL_PROFILE_AGENTS_INVALID")
        template = str(agent.get("installation_id_template") or "")
        if template and template.count("{device_id}") != 1:
            raise DeviceRuntimeError("INSTALL_PROFILE_AGENT_TEMPLATE_INVALID")
    release = profile.get("release")
    if release is not None:
        if not isinstance(release, dict) or set(release) != {"release_id", "archive_url", "sha256"}:
            raise DeviceRuntimeError("INSTALL_PROFILE_RELEASE_INVALID")
        archive = urlsplit(str(release.get("archive_url") or ""))
        try:
            archive_port = archive.port
        except ValueError as exc:
            raise DeviceRuntimeError("INSTALL_PROFILE_RELEASE_INVALID") from exc
        if (
            archive.scheme != "https"
            or not archive.netloc
            or archive.username
            or archive.password
            or (archive_port is not None and not 1 <= archive_port <= 65535)
            or archive.query
            or archive.fragment
            or re.fullmatch(r"[A-Za-z0-9._-]{1,96}", str(release.get("release_id") or "")) is None
            or re.fullmatch(r"[0-9a-fA-F]{64}", str(release.get("sha256") or "")) is None
        ):
            raise DeviceRuntimeError("INSTALL_PROFILE_RELEASE_INVALID")
    return profile


def load_profile(path: Path) -> dict[str, Any]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DeviceRuntimeError("INSTALL_PROFILE_UNREADABLE") from exc
    return validate_profile(profile)


def detect_agent_types(environment: Mapping[str, str] | None = None) -> list[str]:
    """通过命令和常见配置目录发现可接入客户端，不读取客户端内容。"""

    env = dict(os.environ if environment is None else environment)
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
    roaming = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    signals = {
        "codex": [home / ".codex"],
        "hermes": [home / ".hermes", local / "hermes", local / "Hermes Studio", roaming / "hermes"],
        "openclaw": [home / ".openclaw", local / "openclaw", roaming / "openclaw"],
    }
    return [
        agent_type
        for agent_type in ("codex", "hermes", "openclaw")
        if shutil.which(agent_type, path=env.get("PATH")) is not None
        or any(path.exists() for path in signals[agent_type])
    ]


def _prompt_value(prompt: str, current: str | None = None) -> str:
    suffix = f" [{current}]" if current else ""
    value = input(f"{prompt}{suffix}：").strip()
    return value or str(current or "").strip()


def _onboard_profile(args: argparse.Namespace) -> dict[str, Any]:
    if args.profile is not None:
        profile = load_profile(args.profile)
        if args.gateway_url or args.workspace or args.agent:
            raise DeviceRuntimeError("ONBOARD_PROFILE_CONFLICT")
        return profile

    gateway_url = str(args.gateway_url or "").strip()
    workspace = str(args.workspace or "").strip()
    selected_agents = list(args.agent or ())
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive:
        print("共享记忆设备配置")
        print("配对码、刷新凭据和私钥不会写入安装配置。")
        gateway_url = _prompt_value("Gateway HTTPS 地址", gateway_url)
        workspace = _prompt_value("默认工作区 ID", workspace)
        detected = detect_agent_types()
        default_agents = ",".join(detected) or "codex"
        raw_agents = _prompt_value(
            "接入的 Agent（codex, hermes, openclaw，以逗号分隔）",
            ",".join(selected_agents) or default_agents,
        )
        selected_agents = [value.strip().lower() for value in raw_agents.split(",") if value.strip()]
    if not gateway_url or not workspace or not selected_agents:
        raise DeviceRuntimeError("ONBOARD_REQUIRED_VALUES_MISSING")
    if len(set(selected_agents)) != len(selected_agents) or any(
        value not in AGENT_DISPLAY_NAMES for value in selected_agents
    ):
        raise DeviceRuntimeError("ONBOARD_AGENT_TYPES_INVALID")
    selected = args.platform or current_platform()
    return validate_profile(
        {
            "version": 1,
            "gateway_url": gateway_url,
            "default_workspace": workspace,
            "device_id_prefix": selected,
            "agents": [
                {
                    "type": agent_type,
                    "display_name": AGENT_DISPLAY_NAMES[agent_type],
                    "installation_id_template": f"{agent_type}-{{device_id}}",
                }
                for agent_type in selected_agents
            ],
        }
    )


def _pairing_code(pairing_code_stdin: bool) -> str:
    value = (
        sys.stdin.readline().rstrip("\r\n")
        if pairing_code_stdin
        else getpass.getpass("请输入管理员生成的一次性配对码：")
    )
    if not value:
        raise DeviceRuntimeError("PAIRING_CODE_REQUIRED")
    return value


def onboard_device(args: argparse.Namespace) -> dict[str, Any]:
    profile = _onboard_profile(args)
    pairing_code = _pairing_code(args.pairing_code_stdin)
    try:
        return install_device(
            profile,
            pairing_code=pairing_code,
            platform_name=args.platform,
            device_id=args.device_id,
            device_name=args.device_name,
            python_executable=args.python_executable,
            credential_username=args.credential_username,
            gateway_ca_certificate=args.gateway_ca_certificate,
            port=args.port,
            enable_autostart=not args.no_autostart,
            resume=args.resume,
        )
    finally:
        pairing_code = ""


def _quote_systemd(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _sidecar_environment(config: Mapping[str, Any]) -> dict[str, str]:
    environment = {
        "MEMORY_DEVICE_RUNTIME_CONFIG": str(config["runtime_config_file"]),
    }
    return environment


def render_systemd_user_unit(config: Mapping[str, Any]) -> str:
    lines = [
        "[Unit]",
        "Description=Agent Memory Gateway Sidecar",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
    ]
    for key, value in sorted(_sidecar_environment(config).items()):
        lines.append(f"Environment={_quote_systemd(f'{key}={value}')}" )
    lines.extend(
        [
            f"ExecStart={_quote_systemd(str(config['python_executable']))} -m agent_memory_gateway.sidecar_daemon --host 127.0.0.1 --port {int(config.get('port') or 8766)}",
            "Restart=on-failure",
            "RestartSec=3",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return "\n".join(lines)


def render_launchd_plist(config: Mapping[str, Any]) -> str:
    environment = "\n".join(
        f"      <key>{_xml(key)}</key><string>{_xml(value)}</string>"
        for key, value in sorted(_sidecar_environment(config).items())
    )
    arguments = "".join(
        f"<string>{_xml(value)}</string>"
        for value in (
            str(config["python_executable"]),
            "-m",
            "agent_memory_gateway.sidecar_daemon",
            "--host",
            "127.0.0.1",
            "--port",
            str(int(config.get("port") or 8766)),
        )
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agentmemory.gateway.sidecar</string>
  <key>ProgramArguments</key><array>{arguments}</array>
  <key>EnvironmentVariables</key><dict>
{environment}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
"""


def render_windows_service_manifest(config: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "version": 1,
            "managed_by": "agent-memory-gateway",
            "task_name": "MemoryGatewaySidecar",
            "command": [
                str(config["python_executable"]),
                "-m",
                "agent_memory_gateway.sidecar_daemon",
                "--runtime-config",
                str(config["runtime_config_file"]),
                "--host",
                "127.0.0.1",
                "--port",
                str(int(config.get("port") or 8766)),
            ],
            "environment": _sidecar_environment(config),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _atomic_write(path: Path, text: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            raise FileExistsError(f"拒绝覆盖已有文件：{path}") from None
        temporary_path.unlink()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path.exists():
            temporary_path.unlink()


def _write_or_verify(path: Path, text: str, *, private: bool, resume: bool) -> None:
    if not path.exists():
        _atomic_write(path, text, private=private)
        return
    if not resume:
        raise FileExistsError(f"拒绝覆盖已有文件：{path}")
    try:
        existing = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DeviceRuntimeError("INSTALL_EXISTING_FILE_UNREADABLE") from exc
    if existing != text:
        raise DeviceRuntimeError(f"INSTALL_RESUME_CONFLICT:{path}")


def assert_service_slot_available(platform_name: str, output: Path) -> None:
    """新安装不得接管同名但不由当前安装管理的用户服务。"""

    if platform_name == "linux":
        loaded = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                output.name,
                "--property=FragmentPath",
                "--value",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if loaded.returncode == 0 and str(loaded.stdout or "").strip():
            raise DeviceRuntimeError("LINUX_SERVICE_ALREADY_ENABLED")
        return
    if platform_name == "macos":
        target = f"gui/{os.getuid()}"
        label = "com.agentmemory.gateway.sidecar"
        loaded = subprocess.run(
            ["launchctl", "print", f"{target}/{label}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if loaded.returncode == 0:
            raise DeviceRuntimeError("MACOS_SERVICE_ALREADY_LOADED")
        return
    if platform_name == "windows":
        loaded = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", "MemoryGatewaySidecar"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if loaded.returncode == 0:
            raise DeviceRuntimeError("WINDOWS_TASK_ALREADY_EXISTS")
        return
    raise DeviceRuntimeError("PLATFORM_UNSUPPORTED")


def enable_service_definition(platform_name: str, output: Path) -> None:
    """启用或重新启动现有用户服务，允许安装中断后安全重试。"""

    if platform_name == "linux":
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", output.name], check=True)
        return
    if platform_name == "macos":
        target = f"gui/{os.getuid()}"
        label = "com.agentmemory.gateway.sidecar"
        loaded = subprocess.run(
            ["launchctl", "print", f"{target}/{label}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if loaded.returncode == 0:
            subprocess.run(["launchctl", "kickstart", "-k", f"{target}/{label}"], check=True)
        else:
            subprocess.run(["launchctl", "bootstrap", target, str(output)], check=True)
        return
    if platform_name == "windows":
        try:
            manifest = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise DeviceRuntimeError("WINDOWS_SERVICE_MANIFEST_INVALID") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("managed_by") != "agent-memory-gateway"
            or manifest.get("version") != 1
            or not isinstance(manifest.get("command"), list)
        ):
            raise DeviceRuntimeError("WINDOWS_SERVICE_MANIFEST_INVALID")
        task_name = str(manifest.get("task_name") or "")
        if task_name != "MemoryGatewaySidecar":
            raise DeviceRuntimeError("WINDOWS_SERVICE_MANIFEST_INVALID")
        existing = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name, "/XML"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if existing.returncode == 0:
            xml = str(existing.stdout or "")
            runtime_marker = str(manifest["command"][4])
            if "agent_memory_gateway.sidecar_daemon" not in xml or runtime_marker not in xml:
                raise DeviceRuntimeError("WINDOWS_TASK_NOT_MANAGED")
        else:
            command = [str(value) for value in manifest["command"]]
            domain = str(os.environ.get("USERDOMAIN") or "").strip()
            username = str(os.environ.get("USERNAME") or getpass.getuser()).strip()
            user_id = f"{domain}\\{username}" if domain else username
            arguments = subprocess.list2cmdline(command[1:])
            task_xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{_xml("启动仅回环访问的 Memory Gateway Sidecar；内部 CA 仅在该进程中使用。")}</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{_xml(user_id)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{_xml(user_id)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
  <Actions Context="Author"><Exec><Command>{_xml(command[0])}</Command><Arguments>{_xml(arguments)}</Arguments><WorkingDirectory>{_xml(str(Path(command[0]).parent))}</WorkingDirectory></Exec></Actions>
</Task>
'''
            descriptor, temporary = tempfile.mkstemp(
                prefix=".memory-gateway-task-", suffix=".xml", dir=output.parent
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-16", newline="\r\n") as stream:
                    descriptor = -1
                    stream.write(task_xml)
                subprocess.run(
                    ["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(temporary_path)],
                    check=True,
                )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary_path.unlink(missing_ok=True)
        subprocess.run(["schtasks.exe", "/Run", "/TN", task_name], check=True)
        return
    raise DeviceRuntimeError("PLATFORM_UNSUPPORTED")


def write_service_definition(
    config: Mapping[str, Any],
    platform_name: str,
    output: Path,
    *,
    enable: bool,
) -> dict[str, Any]:
    if platform_name == "linux":
        text = render_systemd_user_unit(config)
    elif platform_name == "macos":
        text = render_launchd_plist(config)
    elif platform_name == "windows":
        text = render_windows_service_manifest(config)
    else:
        raise DeviceRuntimeError("PLATFORM_UNSUPPORTED")
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已有自启动配置：{output}")
    if enable:
        assert_service_slot_available(platform_name, output)
    _atomic_write(output, text, private=platform_name == "windows")
    if enable:
        enable_service_definition(platform_name, output)
    return {"platform": platform_name, "service_file": str(output), "enabled": bool(enable)}


def load_runtime_environment(path: Path) -> dict[str, str]:
    """从所有者专用 JSON 读取 Sidecar 配置；服务定义只引用此文件。"""

    try:
        file_stat = path.stat()
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DeviceRuntimeError("DEVICE_RUNTIME_CONFIG_INVALID") from exc
    if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise DeviceRuntimeError("DEVICE_RUNTIME_CONFIG_PERMISSIONS_INVALID")
    if not isinstance(config, dict):
        raise DeviceRuntimeError("DEVICE_RUNTIME_CONFIG_INVALID")
    required = {
        "gateway_url",
        "agent_installation_ids",
        "heartbeat_agent",
        "device_id",
        "default_workspace",
        "sidecar_key_file",
        "memory_home",
    }
    if not required.issubset(config):
        raise DeviceRuntimeError("DEVICE_RUNTIME_CONFIG_INVALID")
    credential_file = str(config.get("credential_file") or "").strip()
    credential_target = str(config.get("credential_target") or "").strip()
    if bool(credential_file) == bool(credential_target):
        raise DeviceRuntimeError("DEVICE_RUNTIME_CONFIG_INVALID")
    agents = config.get("agent_installation_ids")
    if not isinstance(agents, list) or not agents or any(
        IDENTIFIER.fullmatch(str(value)) is None for value in agents
    ):
        raise DeviceRuntimeError("DEVICE_RUNTIME_CONFIG_INVALID")
    from .memory_app import load_sidecar_environment

    sidecar_keys = load_sidecar_environment(
        Path(str(config["sidecar_key_file"])),
        require_private_permissions=os.name != "nt",
    )
    environment = {
        "MEMORY_GATEWAY_URL": str(config["gateway_url"]),
        "MEMORY_SIDECAR_ALLOWED_AGENTS": ",".join(str(value) for value in agents),
        "MEMORY_HEARTBEAT_AGENT": str(config["heartbeat_agent"]),
        "MEMORY_DEVICE_ID": str(config["device_id"]),
        "MEMORY_DEFAULT_WORKSPACE": str(config["default_workspace"]),
        "MEMORY_HOME": str(config["memory_home"]),
        "MEMORY_SIDECAR_PORT": str(config.get("port") or 8766),
        **sidecar_keys,
    }
    if credential_file:
        environment["MEMORY_REFRESH_CREDENTIAL_FILE"] = credential_file
    else:
        environment["MEMORY_REFRESH_CREDENTIAL_TARGET"] = credential_target
    ca_certificate = str(config.get("gateway_ca_certificate") or "")
    if ca_certificate:
        environment["MEMORY_GATEWAY_CA_CERTIFICATE"] = ca_certificate
    return environment


def render_mcp_config(
    *,
    python_executable: str,
    agent_installation_id: str,
    agent_type: str = "other",
    workspace_id: str,
    sidecar_key_file: Path,
    port: int,
) -> dict[str, Any]:
    server = {
        "command": python_executable,
        "args": ["-m", "agent_memory_gateway.sidecar_mcp"],
        "env": {
            "MEMORY_AGENT_INSTALLATION_ID": agent_installation_id,
            "MEMORY_DEFAULT_WORKSPACE": workspace_id,
            "MEMORY_SIDECAR_KEY_FILE": str(sidecar_key_file),
            "MEMORY_SIDECAR_PORT": str(port),
        },
    }
    if agent_type == "openclaw":
        return {"mcp": {"servers": {"shared-memory": server}}}
    return {"mcp_servers": {"shared-memory": server}}


def _installation_identity(
    profile: Mapping[str, Any],
    platform_name: str,
    *,
    device_id: str | None,
    device_name: str | None,
) -> tuple[str, str, list[DevicePairAgent]]:
    hostname = socket.gethostname().strip() or platform_name
    selected_name = str(device_name or hostname).strip()
    if not selected_name or len(selected_name) > 256:
        raise DeviceRuntimeError("INSTALL_DEVICE_NAME_INVALID")
    if device_id:
        selected_id = str(device_id).strip()
    else:
        prefix = str(profile.get("device_id_prefix") or platform_name).strip()
        hostname_id = re.sub(r"[^A-Za-z0-9_.@:-]+", "-", hostname).strip("-")
        selected_id = f"{prefix}-{hostname_id}"
    if IDENTIFIER.fullmatch(selected_id) is None:
        raise DeviceRuntimeError("INSTALL_DEVICE_ID_INVALID")

    agents: list[DevicePairAgent] = []
    for raw in profile["agents"]:
        template = str(raw.get("installation_id_template") or f"{raw['type']}-{{device_id}}")
        installation_id = template.replace("{device_id}", selected_id)
        if IDENTIFIER.fullmatch(installation_id) is None:
            raise DeviceRuntimeError("INSTALL_AGENT_ID_INVALID")
        agents.append(
            DevicePairAgent(
                agent_installation_id=installation_id,
                agent_type=str(raw["type"]),
                display_name=str(raw["display_name"]).strip(),
            )
        )
    if len({agent.agent_installation_id for agent in agents}) != len(agents):
        raise DeviceRuntimeError("INSTALL_AGENT_ID_DUPLICATE")
    return selected_id, selected_name, agents


def _runtime_config(
    *,
    profile: Mapping[str, Any],
    platform_name: str,
    paths: PlatformPaths,
    python_executable: str,
    device_id: str,
    agents: Sequence[DevicePairAgent],
    mcp_files: Sequence[Path],
    sidecar_key_file: Path,
    device_key_file: Path,
    credential_file: Path | None,
    credential_target: str | None,
    gateway_ca_certificate: Path | None,
    port: int,
) -> dict[str, Any]:
    runtime_file = paths.config_dir / "runtime.json"
    config: dict[str, Any] = {
        "version": 1,
        "platform": platform_name,
        "runtime_config_file": str(runtime_file),
        "python_executable": python_executable,
        "gateway_url": str(profile["gateway_url"]),
        "agent_installation_ids": [agent.agent_installation_id for agent in agents],
        "agents": [
            {
                "installation_id": agent.agent_installation_id,
                "type": agent.agent_type,
                "display_name": agent.display_name,
            }
            for agent in agents
        ],
        "heartbeat_agent": agents[0].agent_installation_id,
        "device_id": device_id,
        "default_workspace": str(profile["default_workspace"]),
        "sidecar_key_file": str(sidecar_key_file),
        "device_key_file": str(device_key_file),
        "memory_home": str(paths.state_dir / "sidecar-v1"),
        "port": port,
        "mcp_config_files": [str(path) for path in mcp_files],
        "service_task_name": "MemoryGatewaySidecar",
    }
    if credential_file is not None:
        config["credential_file"] = str(credential_file)
    if credential_target:
        config["credential_target"] = credential_target
    if gateway_ca_certificate is not None:
        config["gateway_ca_certificate"] = str(gateway_ca_certificate)
    return config


def _existing_credential(
    *, credential_file: Path | None, credential_target: str | None
) -> bool:
    if credential_file is not None:
        return read_file_credential(credential_file) is not None
    return read_generic_credential(str(credential_target)) is not None


def verify_sidecar_ready(
    *,
    sidecar_key_file: Path,
    port: int,
    agent_installation_id: str,
    workspace_id: str,
    timeout_seconds: float = 10.0,
) -> None:
    from .memory_app import load_sidecar_environment

    encoded_key = load_sidecar_environment(
        sidecar_key_file, require_private_permissions=os.name != "nt"
    )["MEMORY_OUTBOX_KEY"]
    proxy = LocalSidecarProxy(
        f"http://127.0.0.1:{port}",
        daemon_auth_token(encoded_key),
        agent_installation_id,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proxy.health():
            result = proxy.sync(workspace_id)
            errors = result.get("errors")
            if result.get("offline") is not True and not errors:
                return
            code = str(errors[0]) if isinstance(errors, list) and errors else "GATEWAY_UNAVAILABLE"
            raise DeviceRuntimeError(f"SIDECAR_SYNC_FAILED:{code}")
        time.sleep(0.25)
    raise DeviceRuntimeError("SIDECAR_START_TIMEOUT")


def install_device(
    profile: Mapping[str, Any],
    *,
    pairing_code: str,
    platform_name: str | None = None,
    paths: PlatformPaths | None = None,
    device_id: str | None = None,
    device_name: str | None = None,
    python_executable: str = sys.executable,
    credential_username: str | None = None,
    gateway_ca_certificate: Path | None = None,
    port: int = 8766,
    enable_autostart: bool = True,
    resume: bool = False,
    pairer: Any = pair_device,
    verify_ready: Any = verify_sidecar_ready,
) -> dict[str, Any]:
    """非交互核心安装流程；CLI 负责从隐藏输入读取一次性配对码。"""

    profile = validate_profile(dict(profile))
    selected = platform_name or current_platform()
    if selected not in ALLOWED_PLATFORMS:
        raise DeviceRuntimeError("PLATFORM_UNSUPPORTED")
    if not 1024 <= int(port) <= 65535:
        raise DeviceRuntimeError("INSTALL_PORT_INVALID")
    selected_paths = paths or platform_paths(selected)
    selected_id, selected_name, agents = _installation_identity(
        profile, selected, device_id=device_id, device_name=device_name
    )
    username = str(credential_username or getpass.getuser()).strip()
    if IDENTIFIER.fullmatch(username) is None:
        raise DeviceRuntimeError("INSTALL_CREDENTIAL_USERNAME_INVALID")
    if gateway_ca_certificate is not None:
        if not gateway_ca_certificate.is_file():
            raise DeviceRuntimeError("GATEWAY_CA_CERTIFICATE_MISSING")
        gateway_ca_certificate = gateway_ca_certificate.resolve()

    secrets_dir = selected_paths.config_dir / "secrets"
    sidecar_key_file = secrets_dir / "sidecar.env"
    device_key_file = secrets_dir / "device-identity.pem"
    credential_file = None if selected == "windows" else secrets_dir / "device-refresh.json"
    credential_target = f"AgentMemoryGateway/{selected_id}" if selected == "windows" else None
    runtime_file = selected_paths.config_dir / "runtime.json"
    mcp_dir = selected_paths.data_dir / "mcp"
    mcp_files = [mcp_dir / f"{agent.agent_installation_id}-mcp.json" for agent in agents]

    if resume:
        if not sidecar_key_file.exists():
            generate_sidecar_key_file(sidecar_key_file)
        if not sidecar_key_file.is_file():
            raise DeviceRuntimeError("INSTALL_RESUME_STATE_INCOMPLETE")
        from .memory_app import load_sidecar_environment

        load_sidecar_environment(sidecar_key_file, require_private_permissions=os.name != "nt")
        credential_exists = _existing_credential(
            credential_file=credential_file, credential_target=credential_target
        )
        if credential_exists and not device_key_file.is_file():
            raise DeviceRuntimeError("INSTALL_RESUME_STATE_INCOMPLETE")
        if device_key_file.exists():
            validate_device_key_file(device_key_file)
        if not credential_exists:
            pairer(
                gateway_url=str(profile["gateway_url"]),
                pairing_code=pairing_code,
                device_id=selected_id,
                device_name=selected_name,
                device_type=selected,
                device_key_file=device_key_file,
                agents=agents,
                credential_target=credential_target,
                credential_username=username,
                credential_file=credential_file,
                ca_certificate=gateway_ca_certificate,
            )
    else:
        occupied = [runtime_file, selected_paths.service_file, sidecar_key_file, device_key_file, *mcp_files]
        if any(path.exists() for path in occupied) or sidecar_key_file.exists() or (
            credential_file is not None and credential_file.exists()
        ) or (
            selected == "windows"
            and _existing_credential(
                credential_file=credential_file, credential_target=credential_target
            )
        ):
            raise FileExistsError("安装目标已存在；确认是同一设备后使用 --resume")
        generate_sidecar_key_file(sidecar_key_file)
        pairer(
            gateway_url=str(profile["gateway_url"]),
            pairing_code=pairing_code,
            device_id=selected_id,
            device_name=selected_name,
            device_type=selected,
            device_key_file=device_key_file,
            agents=agents,
            credential_target=credential_target,
            credential_username=username,
            credential_file=credential_file,
            ca_certificate=gateway_ca_certificate,
        )

    runtime = _runtime_config(
        profile=profile,
        platform_name=selected,
        paths=selected_paths,
        python_executable=python_executable,
        device_id=selected_id,
        agents=agents,
        mcp_files=mcp_files,
        sidecar_key_file=sidecar_key_file,
        device_key_file=device_key_file,
        credential_file=credential_file,
        credential_target=credential_target,
        gateway_ca_certificate=gateway_ca_certificate,
        port=port,
    )
    _write_or_verify(
        runtime_file,
        json.dumps(runtime, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        private=True,
        resume=resume,
    )
    for agent, output in zip(agents, mcp_files, strict=True):
        mcp = render_mcp_config(
            python_executable=python_executable,
            agent_installation_id=agent.agent_installation_id,
            agent_type=agent.agent_type,
            workspace_id=str(profile["default_workspace"]),
            sidecar_key_file=sidecar_key_file,
            port=port,
        )
        _write_or_verify(
            output,
            json.dumps(mcp, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            private=True,
            resume=resume,
        )

    service_config = {
        "runtime_config_file": str(runtime_file),
        "python_executable": python_executable,
        "port": port,
    }
    if selected_paths.service_file.exists() and resume:
        expected = (
            render_systemd_user_unit(service_config)
            if selected == "linux"
            else render_launchd_plist(service_config)
            if selected == "macos"
            else render_windows_service_manifest(service_config)
        )
        if selected_paths.service_file.read_text(encoding="utf-8") != expected:
            raise DeviceRuntimeError(f"INSTALL_RESUME_CONFLICT:{selected_paths.service_file}")
        service_result = {
            "platform": selected,
            "service_file": str(selected_paths.service_file),
            "enabled": False,
        }
        if enable_autostart:
            enable_service_definition(selected, selected_paths.service_file)
            service_result["enabled"] = True
    else:
        service_result = write_service_definition(
            service_config,
            selected,
            selected_paths.service_file,
            enable=enable_autostart,
        )
    if enable_autostart:
        verify_ready(
            sidecar_key_file=sidecar_key_file,
            port=port,
            agent_installation_id=agents[0].agent_installation_id,
            workspace_id=str(profile["default_workspace"]),
        )

    return {
        "status": "ready" if enable_autostart else "configured",
        "platform": selected,
        "device_id": selected_id,
        "agent_installation_ids": [agent.agent_installation_id for agent in agents],
        "runtime_config_file": str(runtime_file),
        "service_file": service_result["service_file"],
        "sidecar_autostart": "enabled" if enable_autostart else "not_enabled",
        "gateway_sync": "ready" if enable_autostart else "not_run",
        "mcp_config_files": [str(path) for path in mcp_files],
        "client_configuration": [
            {
                "agent_installation_id": agent.agent_installation_id,
                "agent_type": agent.agent_type,
                "config_file": str(path),
                "status": "generated_not_imported",
            }
            for agent, path in zip(agents, mcp_files, strict=True)
        ],
        "next_step": "将对应 JSON 文件中的 shared-memory 服务合并到 Agent 的 MCP 配置，然后重启 Agent。",
        "resumed": resume,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="安装、检查和维护共享记忆设备")
    commands = parser.add_subparsers(dest="command", required=True)
    paths_cmd = commands.add_parser("paths", help="输出当前平台默认目录")
    paths_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))

    profile_cmd = commands.add_parser("validate-profile", help="校验不含敏感字段的安装配置")
    profile_cmd.add_argument("--profile", type=Path, required=True)

    service_cmd = commands.add_parser("service", help="生成并可选启用当前平台自启动配置")
    service_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))
    service_cmd.add_argument("--config", type=Path, required=True, help="受保护的本机运行配置 JSON")
    service_cmd.add_argument("--output", type=Path)
    service_cmd.add_argument("--enable", action="store_true")
    install_cmd = commands.add_parser("install", help="使用非敏感配置完成设备配对和 Sidecar 安装")
    install_cmd.add_argument("--profile", type=Path, required=True)
    install_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))
    install_cmd.add_argument("--pairing-code-stdin", action="store_true")
    install_cmd.add_argument("--device-id")
    install_cmd.add_argument("--device-name")
    install_cmd.add_argument("--credential-username")
    install_cmd.add_argument("--gateway-ca-certificate", type=Path)
    install_cmd.add_argument("--python-executable", default=sys.executable)
    install_cmd.add_argument("--port", type=int, default=8766)
    install_cmd.add_argument("--no-autostart", action="store_true")
    install_cmd.add_argument("--resume", action="store_true")

    onboard_cmd = commands.add_parser("onboard", help="交互式完成首次安装和配置")
    onboard_cmd.add_argument("--profile", type=Path)
    onboard_cmd.add_argument("--gateway-url")
    onboard_cmd.add_argument("--workspace")
    onboard_cmd.add_argument(
        "--agent",
        action="append",
        choices=sorted(AGENT_DISPLAY_NAMES),
        help="可重复指定；交互模式会自动检测 Codex、Hermes 和 OpenClaw",
    )
    onboard_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))
    onboard_cmd.add_argument("--pairing-code-stdin", action="store_true")
    onboard_cmd.add_argument("--device-id")
    onboard_cmd.add_argument("--device-name")
    onboard_cmd.add_argument("--credential-username")
    onboard_cmd.add_argument("--gateway-ca-certificate", type=Path)
    onboard_cmd.add_argument("--python-executable", default=sys.executable)
    onboard_cmd.add_argument("--port", type=int, default=8766)
    onboard_cmd.add_argument("--no-autostart", action="store_true")
    onboard_cmd.add_argument("--resume", action="store_true")

    status_cmd = commands.add_parser("status", help="查看本机配置、服务和 Sidecar 状态")
    status_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))

    doctor_cmd = commands.add_parser("doctor", help="诊断本机安装，不修改系统")
    doctor_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))

    repair_cmd = commands.add_parser("repair", help="生成修复计划；确认后执行安全修复")
    repair_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))
    repair_cmd.add_argument("--apply", action="store_true", help="执行修复；默认只预览")

    uninstall_cmd = commands.add_parser("uninstall", help="预览或执行安全卸载")
    uninstall_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))
    uninstall_cmd.add_argument("--yes", action="store_true", help="确认执行；默认只预览")
    uninstall_cmd.add_argument("--purge-credentials", action="store_true", help="同时删除设备身份和凭据")
    uninstall_cmd.add_argument("--purge-data", action="store_true", help="同时删除本地队列和缓存")
    upgrade_cmd = commands.add_parser("upgrade", help="在独立运行环境安装新版本，健康检查通过后切换")
    upgrade_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))
    upgrade_cmd.add_argument("--package", type=Path, required=True, help="已校验的源码目录或 wheel")
    upgrade_cmd.add_argument("--release-id", required=True, help="不可变发布标识，例如 v0.2.0")
    upgrade_cmd.add_argument("--yes", action="store_true", help="确认执行；默认只预览")

    rollback_cmd = commands.add_parser("rollback", help="回到升级前保留的上一个运行版本")
    rollback_cmd.add_argument("--platform", choices=sorted(ALLOWED_PLATFORMS))
    rollback_cmd.add_argument("--yes", action="store_true", help="确认执行；默认只预览")
    args = parser.parse_args(argv)

    try:
        if args.command == "paths":
            selected = args.platform or current_platform()
            paths = platform_paths(selected)
            result = {"platform": selected, **{key: str(value) for key, value in vars(paths).items()}}
        elif args.command == "validate-profile":
            result = {"status": "valid", "profile": load_profile(args.profile)}
        elif args.command == "service":
            selected = args.platform or current_platform()
            config = json.loads(args.config.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise DeviceRuntimeError("DEVICE_RUNTIME_CONFIG_INVALID")
            output = args.output or platform_paths(selected).service_file
            result = write_service_definition(config, selected, output, enable=args.enable)
        elif args.command == "install":
            pairing_code = _pairing_code(args.pairing_code_stdin)
            try:
                result = install_device(
                    load_profile(args.profile),
                    pairing_code=pairing_code,
                    platform_name=args.platform,
                    device_id=args.device_id,
                    device_name=args.device_name,
                    python_executable=args.python_executable,
                    credential_username=args.credential_username,
                    gateway_ca_certificate=args.gateway_ca_certificate,
                    port=args.port,
                    enable_autostart=not args.no_autostart,
                    resume=args.resume,
                )
            finally:
                pairing_code = ""
        elif args.command == "onboard":
            result = onboard_device(args)
        else:
            selected = args.platform or current_platform()
            paths = platform_paths(selected)
            from .device_lifecycle import (
                device_status,
                diagnose_device,
                repair_device,
                rollback_device,
                uninstall_device,
                upgrade_device,
            )

            if args.command == "status":
                result = device_status(selected, paths)
            elif args.command == "doctor":
                result = diagnose_device(selected, paths)
            elif args.command == "repair":
                result = repair_device(selected, paths, apply=args.apply)
            elif args.command == "uninstall":
                result = uninstall_device(
                    selected,
                    paths,
                    apply=args.yes,
                    purge_credentials=args.purge_credentials,
                    purge_data=args.purge_data,
                )
            elif args.command == "upgrade":
                result = upgrade_device(
                    selected,
                    paths,
                    package=args.package,
                    release_id=args.release_id,
                    apply=args.yes,
                )
            else:
                result = rollback_device(selected, paths, apply=args.yes)
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
