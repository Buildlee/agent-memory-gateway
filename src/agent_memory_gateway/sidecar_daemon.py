"""本机共用 Sidecar：一个 outbox 所有者，其他 Agent 通过回环 RPC 调用。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, request

from .sidecar_client import GatewayHTTPError, GatewayTransportError, SidecarClient
from .sidecar_auth import SidecarAuthError, refresh_token_provider_from_environment


MAX_LOCAL_RPC_BYTES = 1_048_576
_owned_servers: list[ThreadingHTTPServer] = []


class SidecarDaemonError(RuntimeError):
    """本机共用 Sidecar 无法建立或调用。"""


def daemon_auth_token(encoded_outbox_key: str) -> str:
    try:
        padding = "=" * (-len(encoded_outbox_key) % 4)
        key = base64.urlsafe_b64decode((encoded_outbox_key + padding).encode("ascii"))
    except (UnicodeError, ValueError) as exc:
        raise SidecarDaemonError("MEMORY_OUTBOX_KEY 格式无效") from exc
    if len(key) != 32:
        raise SidecarDaemonError("MEMORY_OUTBOX_KEY 必须为 256 位")
    return hmac.new(key, b"memory-sidecar-local-rpc-v1", hashlib.sha256).hexdigest()


class _SidecarRPCHandler(BaseHTTPRequestHandler):
    client: Any
    daemon_token: str
    operation_lock: threading.RLock
    token_provider: Any = None
    allowed_agent_ids: frozenset[str] | None = None

    def do_GET(self) -> None:
        if not self._authorized():
            self._json({"error": "LOCAL_AUTH_REQUIRED"}, status=401)
            return
        if self.path == "/health":
            self._json({"ok": True, "service": "memory-sidecar"})
            return
        self._json({"error": "NOT_FOUND"}, status=404)

    def do_POST(self) -> None:
        if not self._authorized():
            self._json({"error": "LOCAL_AUTH_REQUIRED"}, status=401)
            return
        if self.path != "/rpc":
            self._json({"error": "NOT_FOUND"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if not 0 < length <= MAX_LOCAL_RPC_BYTES:
                raise SidecarDaemonError("LOCAL_RPC_SIZE_INVALID")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("payload", {}), dict):
                raise SidecarDaemonError("LOCAL_RPC_INVALID")
            method = str(payload.get("method") or "")
            arguments = payload.get("payload") or {}
            agent_installation_id = str(payload.get("agent_installation_id") or "").strip()
            allowed = {
                "remember": self.client.remember,
                "search": self.client.search,
                "context": self.client.context,
                "feedback": self.client.feedback,
                "forget": self.client.forget,
                "cleanup": self.client.cleanup_confirmed,
            }
            for review_method in ("list_reviews", "resolve_review", "revert_review", "rebuild_crystal"):
                method_impl = getattr(self.client, review_method, None)
                if callable(method_impl):
                    allowed[review_method] = method_impl
            for admin_method in (
                "admin_overview",
                "list_admin_devices",
                "update_admin_binding",
                "revoke_admin_agent",
                "revoke_admin_device",
                "list_admin_audit",
                "list_admin_dead_letters",
                "list_memories",
                "memory_graph",
            ):
                method_impl = getattr(self.client, admin_method, None)
                if callable(method_impl):
                    allowed[admin_method] = method_impl
            with self.operation_lock:
                previous_token = getattr(self.client, "token", None)
                previous_agent_id = getattr(self.client, "agent_id", None)
                token_overridden = False
                try:
                    if self.token_provider is not None:
                        if not agent_installation_id or len(agent_installation_id) > 256:
                            raise SidecarDaemonError("AGENT_INSTALLATION_ID_REQUIRED")
                        if self.allowed_agent_ids is not None and agent_installation_id not in self.allowed_agent_ids:
                            raise SidecarDaemonError("LOCAL_AGENT_FORBIDDEN")
                        self.client.token = self.token_provider.access_token(agent_installation_id)
                        self.client.agent_id = agent_installation_id
                        token_overridden = True
                    if method == "sync":
                        result = self.client.sync(workspace_id=arguments.get("workspace_id"))
                    elif method == "cleanup":
                        result = self.client.cleanup_confirmed(
                            confirmed_by_user=bool(arguments.get("confirmed_by_user"))
                        )
                    elif method in allowed:
                        result = allowed[method](arguments)
                    else:
                        raise SidecarDaemonError("LOCAL_METHOD_UNSUPPORTED")
                finally:
                    if token_overridden:
                        self.client.token = previous_token
                        self.client.agent_id = previous_agent_id
            self._json({"result": result})
        except SidecarAuthError as exc:
            self._json({"error": str(exc)}, status=503)
        except GatewayHTTPError as exc:
            self._json(
                {"error": exc.code, "retryable": exc.retryable},
                status=503 if exc.retryable else 400,
            )
        except GatewayTransportError:
            self._json({"error": "GATEWAY_UNAVAILABLE", "retryable": True}, status=503)
        except (UnicodeError, ValueError, SidecarDaemonError) as exc:
            code = str(exc)
            if not code.isupper():
                code = "LOCAL_RPC_INVALID"
            self._json({"error": code}, status=400)
        except Exception:  # noqa: BLE001
            self._json({"error": "LOCAL_INTERNAL_ERROR"}, status=500)

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization") or ""
        expected = f"Sidecar {self.daemon_token}"
        return hmac.compare_digest(supplied, expected)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def create_sidecar_server(
    client: Any,
    daemon_token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token_provider: Any = None,
    allowed_agent_ids: frozenset[str] | None = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SidecarDaemonError("Sidecar daemon 只能监听回环地址")
    handler = type(
        "ConfiguredSidecarRPCHandler",
        (_SidecarRPCHandler,),
        {
            "client": client,
            "daemon_token": daemon_token,
            "operation_lock": threading.RLock(),
            "token_provider": token_provider,
            "allowed_agent_ids": allowed_agent_ids,
        },
    )
    return ThreadingHTTPServer((host, port), handler)


class LocalSidecarProxy:
    """与 SidecarClient 保持相同方法表面的本机 RPC 客户端。"""

    def __init__(self, url: str, daemon_token: str, agent_installation_id: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.daemon_token = daemon_token
        self.agent_installation_id = str(agent_installation_id or "").strip()

    def health(self) -> bool:
        req = request.Request(
            self.url + "/health",
            headers={"Authorization": f"Sidecar {self.daemon_token}"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=0.5) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
            return payload == {"ok": True, "service": "memory-sidecar"}
        except (error.URLError, TimeoutError, OSError, ValueError):
            return False

    def remember(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("remember", payload)

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("search", payload)

    def context(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("context", payload)

    def feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("feedback", payload)

    def forget(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("forget", payload)

    def list_reviews(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("list_reviews", payload)

    def resolve_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("resolve_review", payload)

    def revert_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("revert_review", payload)

    def rebuild_crystal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("rebuild_crystal", payload)

    def admin_overview(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("admin_overview", payload)

    def list_admin_devices(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("list_admin_devices", payload)

    def update_admin_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("update_admin_binding", payload)

    def revoke_admin_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("revoke_admin_agent", payload)

    def revoke_admin_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("revoke_admin_device", payload)

    def list_admin_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("list_admin_audit", payload)

    def list_admin_dead_letters(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("list_admin_dead_letters", payload)

    def sync(self, workspace_id: str | None = None) -> dict[str, Any]:
        return self._call("sync", {"workspace_id": workspace_id})

    def cleanup_confirmed(self, confirmed_by_user: bool = False) -> dict[str, Any]:
        return self._call("cleanup", {"confirmed_by_user": bool(confirmed_by_user)})

    def list_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("list_memories", payload)

    def memory_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._call("memory_graph", payload)

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(
            {
                "method": method,
                "payload": payload,
                "agent_installation_id": self.agent_installation_id or None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        req = request.Request(
            self.url + "/rpc",
            data=body,
            headers={
                "Authorization": f"Sidecar {self.daemon_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=30) as response:  # noqa: S310
                result = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except (UnicodeError, ValueError):
                result = {}
            raise SidecarDaemonError(str(result.get("error") or "LOCAL_RPC_FAILED")) from None
        except (error.URLError, TimeoutError, OSError):
            raise SidecarDaemonError("LOCAL_SIDECAR_UNAVAILABLE") from None
        if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
            raise SidecarDaemonError("LOCAL_RPC_INVALID_RESPONSE")
        return result["result"]


def get_shared_sidecar() -> LocalSidecarProxy:
    encoded_key = os.environ.get("MEMORY_OUTBOX_KEY", "")
    token = daemon_auth_token(encoded_key)
    port = int(os.environ.get("MEMORY_SIDECAR_PORT", "8766"))
    if not 1024 <= port <= 65535:
        raise SidecarDaemonError("MEMORY_SIDECAR_PORT 无效")
    agent_installation_id = os.environ.get(
        "MEMORY_AGENT_INSTALLATION_ID", os.environ.get("MEMORY_AGENT_ID", "")
    )
    proxy = LocalSidecarProxy(f"http://127.0.0.1:{port}", token, agent_installation_id)
    if proxy.health():
        return proxy

    if os.environ.get("MEMORY_ALLOW_EMBEDDED_SIDECAR") != "1":
        raise SidecarDaemonError("LOCAL_SIDECAR_UNAVAILABLE")

    startup_error: list[BaseException] = []

    def own_daemon() -> None:
        try:
            client = SidecarClient()
            provider = (
                refresh_token_provider_from_environment()
                if (
                    os.environ.get("MEMORY_REFRESH_CREDENTIAL_TARGET")
                    or os.environ.get("MEMORY_REFRESH_CREDENTIAL_FILE")
                )
                else None
            )
            allowed_agent_ids = _allowed_agent_ids_from_environment()
            server = create_sidecar_server(
                client,
                token,
                port=port,
                token_provider=provider,
                allowed_agent_ids=allowed_agent_ids,
            )
            _owned_servers.append(server)
            server.serve_forever()
        except BaseException as exc:  # noqa: BLE001
            startup_error.append(exc)

    threading.Thread(target=own_daemon, daemon=True, name="memory-sidecar-daemon").start()
    for _ in range(40):
        if proxy.health():
            return proxy
        if startup_error and not isinstance(startup_error[0], OSError):
            raise SidecarDaemonError(type(startup_error[0]).__name__) from None
        time.sleep(0.05)
    raise SidecarDaemonError("LOCAL_SIDECAR_START_TIMEOUT")


def _allowed_agent_ids_from_environment() -> frozenset[str] | None:
    raw = os.environ.get("MEMORY_SIDECAR_ALLOWED_AGENTS", "")
    values = frozenset(value.strip() for value in raw.split(",") if value.strip())
    return values or None


def main() -> None:
    """启动独立、仅回环可访问的本机 Sidecar。"""

    parser = argparse.ArgumentParser(description="启动本机共享记忆 Sidecar")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MEMORY_SIDECAR_PORT", "8766")))
    args = parser.parse_args()
    encoded_key = os.environ.get("MEMORY_OUTBOX_KEY", "")
    token = daemon_auth_token(encoded_key)
    provider = refresh_token_provider_from_environment()
    client = SidecarClient()
    server = create_sidecar_server(
        client,
        token,
        host=args.host,
        port=args.port,
        token_provider=provider,
        allowed_agent_ids=_allowed_agent_ids_from_environment(),
    )
    heartbeat_agent = os.environ.get("MEMORY_HEARTBEAT_AGENT", "hermes-desktop")
    print(f"Memory Sidecar listening on http://{args.host}:{args.port}", flush=True)
    def _heartbeat() -> None:
        import time as _time
        while True:
            _time.sleep(300)
            try:
                if provider is not None:
                    token = provider.access_token(heartbeat_agent)
                    client.token = token
                    client.agent_id = heartbeat_agent
                    client.sync()
            except Exception as exc:  # noqa: BLE001
                print(f"Memory Sidecar heartbeat failed: {type(exc).__name__}", flush=True)
    import threading as _threading
    _threading.Thread(target=_heartbeat, daemon=True, name="memory-sidecar-heartbeat").start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
