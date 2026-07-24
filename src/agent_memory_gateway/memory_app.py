"""在一个容器内编排 Gateway、Worker 和中枢管理面。"""

from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Sequence


class MemoryAppError(RuntimeError):
    pass


ALLOWED_SIDECAR_KEYS = frozenset({"MEMORY_OUTBOX_KEY", "MEMORY_OUTBOX_KEY_VERSION"})


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


def run_supervisor(
    commands: Sequence[Sequence[str]],
    *,
    environment: Mapping[str, str],
    poll_seconds: float = 0.5,
) -> int:
    children: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop_children(_signum: int | None = None, _frame: object | None = None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    try:
        for command in commands:
            children.append(subprocess.Popen(tuple(command), env=dict(environment)))
        while not stopping:
            for child in children:
                return_code = child.poll()
                if return_code is not None:
                    stop_children()
                    return return_code if return_code != 0 else 1
            time.sleep(poll_seconds)
    finally:
        stop_children()
        deadline = time.monotonic() + 10
        for child in children:
            if child.poll() is None:
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
        for child in children:
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
    environment = dict(os.environ)
    environment.update(sidecar_environment)
    environment["MEMORY_SIDECAR_PORT"] = "8766"
    commands = build_child_commands(
        python_executable=sys.executable,
        workspace_id=workspace_id,
        public_base_url=public_base_url,
        launch_token_file=args.launch_token_file,
    )
    raise SystemExit(run_supervisor(commands, environment=environment))


if __name__ == "__main__":
    main()
