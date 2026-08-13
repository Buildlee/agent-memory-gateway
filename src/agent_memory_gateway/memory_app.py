"""在一个容器内编排 Gateway、Worker 和中枢管理面。"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable, Mapping, Sequence


class MemoryAppError(RuntimeError):
    pass


ALLOWED_SIDECAR_KEYS = frozenset({"MEMORY_OUTBOX_KEY", "MEMORY_OUTBOX_KEY_VERSION"})
CHILD_PROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HOME",
        "LANG",
        "LD_LIBRARY_PATH",
        "LOGNAME",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "VIRTUAL_ENV",
    }
)
GATEWAY_MEMORY_KEYS = frozenset(
    {
        "MEMORY_ACCESS_TOKEN_KEY_VERSION",
        "MEMORY_ACCESS_TOKEN_SIGNING_KEY",
        "MEMORY_EVENT_ENCRYPTION_KEY",
        "MEMORY_EVENT_KEY_VERSION",
        "MEMORY_GBRAIN_BACKEND_DSN",
        "MEMORY_HTTP_MAX_CONCURRENT_REQUESTS",
        "MEMORY_HTTP_READ_TIMEOUT_SECONDS",
        "MEMORY_METADATA_RUNTIME_DSN",
        "MEMORY_REFRESH_REPLAY_KEY",
        "MEMORY_REFRESH_REPLAY_KEY_VERSION",
        "MEMORY_SENSITIVE_FINGERPRINT_KEY",
        "MEMORY_TRUST_PROXY_X_FORWARDED_FOR",
        "MEMORY_WORKER_HEARTBEAT_MAX_SECONDS",
    }
)
GATEWAY_MEMORY_PREFIXES = ("MEMORY_METADATA_POOL_", "MEMORY_GBRAIN_POOL_")
WORKER_MEMORY_KEYS = frozenset(
    {
        "MEMORY_EVENT_ENCRYPTION_KEY",
        "MEMORY_EVENT_KEY_VERSION",
        "MEMORY_GBRAIN_BACKEND_DSN",
        "MEMORY_METADATA_RUNTIME_DSN",
    }
)
WORKER_MEMORY_PREFIXES = ("MEMORY_WORKER_METADATA_POOL_", "MEMORY_WORKER_GBRAIN_POOL_")
SIDECAR_MEMORY_KEYS = frozenset(
    {
        "MEMORY_AGENT_ID",
        "MEMORY_AGENT_INSTALLATION_ID",
        "MEMORY_DEFAULT_WORKSPACE",
        "MEMORY_DEVICE_ID",
        "MEMORY_GATEWAY_CA_CERTIFICATE",
        "MEMORY_GATEWAY_URL",
        "MEMORY_HEARTBEAT_AGENT",
        "MEMORY_HOME",
        "MEMORY_LOCAL_PROVIDER_CONFIG",
        "MEMORY_OUTBOX_KEY",
        "MEMORY_OUTBOX_KEY_VERSION",
        "MEMORY_REFRESH_CREDENTIAL_FILE",
        "MEMORY_REFRESH_CREDENTIAL_TARGET",
        "MEMORY_SIDECAR_ALLOWED_AGENTS",
        "MEMORY_SIDECAR_PORT",
    }
)
ADMIN_MEMORY_KEYS = frozenset(
    {
        "MEMORY_AGENT_ID",
        "MEMORY_AGENT_INSTALLATION_ID",
        "MEMORY_DEFAULT_WORKSPACE",
        "MEMORY_OUTBOX_KEY",
        "MEMORY_SIDECAR_PORT",
    }
)
MAX_CHILD_RESTARTS = 5
CHILD_RESTART_WINDOW_SECONDS = 60.0
CHILD_RESTART_DELAY_SECONDS = 1.0
MAX_CHILD_RESTART_DELAY_SECONDS = 15.0


def load_sidecar_environment(path: Path, *, require_private_permissions: bool = True) -> dict[str, str]:
    """只从受保护文件读取 Sidecar RPC 密钥，不接受其他环境变量注入。"""

    try:
        file_stat = path.stat()
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MemoryAppError("MEMORY_APP_SIDECAR_STATE_UNREADABLE") from exc
    if require_private_permissions and stat.S_IMODE(file_stat.st_mode) not in {0o600, 0o700}:
        raise MemoryAppError("MEMORY_APP_SIDECAR_STATE_PERMISSIONS")
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise MemoryAppError("MEMORY_APP_SIDECAR_STATE_INVALID")
        key, value = stripped.split("=", 1)
        if key not in ALLOWED_SIDECAR_KEYS or not value:
            raise MemoryAppError("MEMORY_APP_SIDECAR_STATE_INVALID")
        values[key] = value
    if not all(values.get(key) for key in ALLOWED_SIDECAR_KEYS):
        raise MemoryAppError("MEMORY_APP_SIDECAR_STATE_INCOMPLETE")
    return values


def build_child_commands(
    *,
    python_executable: str,
    workspace_id: str,
    public_base_url: str,
    launch_token_file: str,
) -> tuple[tuple[str, ...], ...]:
    return (
        (
            python_executable,
            "-m",
            "agent_memory_gateway.gateway",
            "--host",
            "0.0.0.0",
            "--port",
            "8787",
        ),
        (
            python_executable,
            "-m",
            "agent_memory_gateway.gateway",
            "reconcile",
            "--forever",
            "--poll-interval-seconds",
            "5",
        ),
        (
            python_executable,
            "-m",
            "agent_memory_gateway.sidecar_daemon",
            "--host",
            "127.0.0.1",
            "--port",
            "8766",
        ),
        (
            python_executable,
            "-m",
            "agent_memory_gateway.admin_console",
            "--workspace",
            workspace_id,
            "--host",
            "0.0.0.0",
            "--port",
            "8767",
            "--allow-network",
            "--secure-cookie",
            "--mount-path",
            "/admin",
            "--public-base-url",
            public_base_url,
            "--launch-token-file",
            launch_token_file,
        ),
    )


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = str(environment.get(name) or "").strip()
    if not value:
        raise MemoryAppError(f"MEMORY_APP_ENV_REQUIRED:{name}")
    return value


def _filtered_child_environment(
    environment: Mapping[str, str],
    *,
    allowed_memory_keys: frozenset[str],
    allowed_memory_prefixes: Sequence[str] = (),
) -> dict[str, str]:
    """保留正常进程环境，但只把该子进程需要的 MEMORY_* 配置传下去。"""

    return {
        key: value
        for key, value in environment.items()
        if key in CHILD_PROCESS_ENVIRONMENT_KEYS
        or key.startswith("LC_")
        or key in allowed_memory_keys
        or any(key.startswith(prefix) for prefix in allowed_memory_prefixes)
    }


def build_child_environments(
    environment: Mapping[str, str], sidecar_environment: Mapping[str, str]
) -> tuple[dict[str, str], ...]:
    """为四个运行进程生成最小化环境，避免管理进程继承数据库与刷新密钥。"""

    combined = dict(environment)
    combined.update(sidecar_environment)
    combined["MEMORY_SIDECAR_PORT"] = "8766"
    return (
        _filtered_child_environment(
            combined,
            allowed_memory_keys=GATEWAY_MEMORY_KEYS,
            allowed_memory_prefixes=GATEWAY_MEMORY_PREFIXES,
        ),
        _filtered_child_environment(
            combined,
            allowed_memory_keys=WORKER_MEMORY_KEYS,
            allowed_memory_prefixes=WORKER_MEMORY_PREFIXES,
        ),
        _filtered_child_environment(combined, allowed_memory_keys=SIDECAR_MEMORY_KEYS),
        _filtered_child_environment(combined, allowed_memory_keys=ADMIN_MEMORY_KEYS),
    )


def run_supervisor(
    commands: Sequence[Sequence[str]],
    *,
    child_environments: Sequence[Mapping[str, str]],
    poll_seconds: float = 0.5,
    process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_child_restarts: int = MAX_CHILD_RESTARTS,
    restart_window_seconds: float = CHILD_RESTART_WINDOW_SECONDS,
    restart_delay_seconds: float = CHILD_RESTART_DELAY_SECONDS,
) -> int:
    if max_child_restarts < 1 or restart_window_seconds <= 0 or restart_delay_seconds < 0:
        raise ValueError("MEMORY_APP_RESTART_POLICY_INVALID")
    if len(child_environments) != len(commands):
        raise ValueError("MEMORY_APP_CHILD_ENVIRONMENTS_INVALID")
    children: list[subprocess.Popen[bytes] | None] = []
    restart_history = [deque() for _ in commands]
    restart_after = [0.0 for _ in commands]
    stopping = False

    def start_child(index: int) -> subprocess.Popen[bytes]:
        return process_factory(tuple(commands[index]), env=dict(child_environments[index]))

    def stop_children(_signum: int | None = None, _frame: object | None = None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in children:
            if child is not None and child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    try:
        for index in range(len(commands)):
            children.append(start_child(index))
        while not stopping:
            now = clock()
            for index, child in enumerate(children):
                if child is None:
                    if now >= restart_after[index]:
                        children[index] = start_child(index)
                    continue
                return_code = child.poll()
                if return_code is None:
                    continue
                history = restart_history[index]
                cutoff = now - restart_window_seconds
                while history and history[0] <= cutoff:
                    history.popleft()
                history.append(now)
                if len(history) > max_child_restarts:
                    stop_children()
                    return return_code if return_code != 0 else 1
                delay = min(
                    restart_delay_seconds * (2 ** (len(history) - 1)),
                    MAX_CHILD_RESTART_DELAY_SECONDS,
                )
                restart_after[index] = now + delay
                children[index] = None
                print(
                    f"memory-app child {index} exited ({return_code}); retrying in {delay:.1f}s",
                    flush=True,
                )
            sleep(poll_seconds)
    finally:
        stop_children()
        deadline = time.monotonic() + 10
        for child in children:
            if child is None:
                continue
            if child.poll() is None:
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
        for child in children:
            if child is None:
                continue
            if child.poll() is None:
                child.wait()
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="启动一体化共享记忆应用")
    parser.add_argument("--sidecar-state", default="/state/sidecar.env")
    parser.add_argument("--launch-token-file", default="/admin-state/launch-url")
    args = parser.parse_args(argv)

    workspace_id = _required_environment(os.environ, "MEMORY_DEFAULT_WORKSPACE")
    public_base_url = _required_environment(os.environ, "MEMORY_ADMIN_PUBLIC_BASE_URL")
    for required_path in ("/state/device-identity.pem", "/state/refresh-credential.json", "/admin-state"):
        if not Path(required_path).exists():
            parser.error(f"中枢管理状态不完整：{required_path}")
    try:
        sidecar_environment = load_sidecar_environment(Path(args.sidecar_state))
    except MemoryAppError as exc:
        parser.error(str(exc))
    commands = build_child_commands(
        python_executable=sys.executable,
        workspace_id=workspace_id,
        public_base_url=public_base_url,
        launch_token_file=args.launch_token_file,
    )
    child_environments = build_child_environments(os.environ, sidecar_environment)
    raise SystemExit(run_supervisor(commands, child_environments=child_environments))


if __name__ == "__main__":
    main()
