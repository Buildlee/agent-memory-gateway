"""HTTP Memory Gateway。"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .access_token import AccessTokenSigner
from .admin_service import PostgresAdminService
from .auth import AuthError, PostgresTokenAuthenticator, TokenAuthenticator
from .crypto import EventCipher
from .crystal_service import PostgresCrystalService
from .db_pool import DatabasePoolBusy, PostgresConnectionPool
from .gbrain_backend import GBrainBackend
from .identity_service import PostgresIdentityService
from .metadata_store import PostgresEventLedger
from .metadata_migrations import MigrationError, inspect_metadata_schema
from .query_service import PostgresQueryService
from .rate_limit import SlidingWindowRateLimiter
from .refresh_replay import RefreshReplayCipher
from .review_service import PostgresReviewService
from .security import SensitiveContentScanner
from .event_contract import EventValidationError
from .store import MemoryStore
from .sync_service import PostgresSyncService, SyncProtocolError


MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024


class GatewayHandler(BaseHTTPRequestHandler):
    """最小 HTTP API 处理器。"""

    db_path: Path
    authenticator: TokenAuthenticator
    identity_service: PostgresIdentityService | None = None
    auth_rate_limiter = SlidingWindowRateLimiter()
    event_ledger: PostgresEventLedger | None = None
    query_service: PostgresQueryService | None = None
    review_service: PostgresReviewService | None = None
    crystal_service: PostgresCrystalService | None = None
    sync_service: PostgresSyncService | None = None
    admin_service: PostgresAdminService | None = None
    readiness_probe: Callable[[], None] | None = None

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/v1/health", "/health/live"}:
            self._json({"ok": True, "service": "agent-memory-gateway"})
            return
        if path == "/health/ready":
            probe = type(self).readiness_probe
            if probe is None:
                self._json({"ok": True, "service": "agent-memory-gateway", "mode": "prototype"})
                return
            try:
                probe()
            except Exception:  # 探针不向调用方暴露数据库或内部异常。
                self._json(
                    {"ok": False, "service": "agent-memory-gateway", "error": "dependencies_unavailable"},
                    status=503,
                )
                return
            self._json({"ok": True, "service": "agent-memory-gateway", "mode": "postgres"})
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        store: MemoryStore | None = None
        try:
            path = urlparse(self.path).path
            payload = self._read_json()
            if path in {"/v1/auth/pair", "/v1/auth/refresh"}:
                if self.identity_service is None:
                    self._json({"error": "not_found"}, status=404)
                    return
                limit, window = (5, 600) if path == "/v1/auth/pair" else (20, 60)
                remote_ip = self.client_address[0] if self.client_address else "unknown"
                if not self.auth_rate_limiter.allow(
                    f"{remote_ip}:{path}",
                    limit=limit,
                    window_seconds=window,
                ):
                    raise AuthError("RATE_LIMITED", status=429)
                if path == "/v1/auth/pair":
                    self._json(self.identity_service.pair(payload), status=201)
                else:
                    self._json(self.identity_service.refresh(payload))
                return
            principal = self.authenticator.authenticate(self.headers.get("Authorization"))
            self.authenticator.validate_payload_identity(principal, payload)
            capability = {
                "/v1/events": "memory.write_event",
                "/v1/sync/push": "memory.write_event",
                "/v1/sync/pull": "memory.read_context",
                "/v1/memories/search": "memory.search",
                "/v1/context": "memory.read_context",
                "/v1/memories/feedback": "memory.feedback",
                "/v1/memories/forget": "memory.forget",
                "/v1/reviews/list": "memory.manage",
                "/v1/reviews/resolve": "memory.manage",
                "/v1/reviews/revert": "memory.manage",
                "/v1/crystals/rebuild": "memory.manage",
                "/v1/admin/overview": "memory.manage",
                "/v1/admin/devices/list": "memory.manage",
                "/v1/admin/bindings/update": "memory.manage",
                "/v1/admin/agents/revoke": "memory.manage",
                "/v1/admin/devices/revoke": "memory.manage",
                "/v1/admin/audit/list": "memory.manage",
                "/v1/admin/dead-letters/list": "memory.manage",
            }.get(path)
            if capability is None:
                self._json({"error": "not_found"}, status=404)
                return
            principal.require_capability(capability)
            if path == "/v1/memories/feedback" and payload.get("action") == "pin":
                principal.require_capability("memory.manage")
            if path == "/v1/events":
                if self.event_ledger is not None:
                    self._json(self.event_ledger.record_proposed_event(payload, principal))
                else:
                    store = MemoryStore(self.db_path)
                    self._json(store.record_event(payload, principal))
            elif path == "/v1/sync/push":
                if self.sync_service is None:
                    raise SyncProtocolError("NOT_IMPLEMENTED")
                self._json(self.sync_service.push(payload, principal))
            elif path == "/v1/sync/pull":
                if self.sync_service is None:
                    raise SyncProtocolError("NOT_IMPLEMENTED")
                self._json(self.sync_service.pull(payload, principal))
            elif path == "/v1/memories/search":
                if self.query_service is not None:
                    self._json(self.query_service.search(payload, principal))
                else:
                    store = MemoryStore(self.db_path)
                    self._json({"memories": store.search(payload, principal)})
            elif path == "/v1/context":
                if self.query_service is not None:
                    self._json(self.query_service.context(payload, principal))
                else:
                    store = MemoryStore(self.db_path)
                    self._json(store.context(payload, principal))
            elif path == "/v1/memories/feedback":
                if self.event_ledger is not None:
                    raise ValueError("NOT_IMPLEMENTED")
                store = MemoryStore(self.db_path)
                self._json(store.feedback(payload, principal))
            elif path == "/v1/memories/forget":
                if self.event_ledger is not None:
                    raise ValueError("NOT_IMPLEMENTED")
                store = MemoryStore(self.db_path)
                self._json(store.forget(payload, principal))
            elif path == "/v1/reviews/list":
                if self.review_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.review_service.list_pending(payload, principal))
            elif path == "/v1/reviews/resolve":
                if self.review_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.review_service.resolve(payload, principal))
            elif path == "/v1/reviews/revert":
                if self.review_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.review_service.revert(payload, principal))
            elif path == "/v1/crystals/rebuild":
                if self.crystal_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.crystal_service.rebuild(payload, principal))
            elif path == "/v1/admin/overview":
                if self.admin_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.admin_service.overview(payload, principal))
            elif path == "/v1/admin/devices/list":
                if self.admin_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.admin_service.list_devices(payload, principal))
            elif path == "/v1/admin/bindings/update":
                if self.admin_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.admin_service.update_binding(payload, principal))
            elif path == "/v1/admin/agents/revoke":
                if self.admin_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.admin_service.revoke_agent(payload, principal))
            elif path == "/v1/admin/devices/revoke":
                if self.admin_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.admin_service.revoke_device(payload, principal))
            elif path == "/v1/admin/audit/list":
                if self.admin_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.admin_service.list_audit(payload, principal))
            elif path == "/v1/admin/dead-letters/list":
                if self.admin_service is None:
                    raise ValueError("NOT_IMPLEMENTED")
                self._json(self.admin_service.list_dead_letters(payload, principal))
        except AuthError as exc:
            self._json({"error": exc.code}, status=exc.status)
        except DatabasePoolBusy as exc:
            self._json({"error": exc.code, "retryable": True}, status=503)
        except SyncProtocolError as exc:
            self._json({"error": exc.code, "retryable": False}, status=400)
        except ValueError as exc:
            if isinstance(exc, EventValidationError):
                code = exc.code
            else:
                candidate = str(exc)
                code = candidate if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", candidate) else "INVALID_REQUEST"
            self._json({"error": code}, status=400)
        except Exception:  # noqa: BLE001
            self._json({"error": "internal_error"}, status=500)
        finally:
            if store is not None:
                store.close()

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise EventValidationError("CONTENT_LENGTH_INVALID") from exc
        if length <= 0:
            raise EventValidationError("CONTENT_LENGTH_REQUIRED")
        if length > MAX_REQUEST_BODY_BYTES:
            raise EventValidationError("REQUEST_BODY_TOO_LARGE")
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    if sys.argv[1:2] == ["doctor"]:
        from .doctor import main as doctor_main

        doctor_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["migrate"]:
        from .migrate import main as migrate_main

        migrate_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["gbrain-migrate"]:
        from .gbrain_migrate import main as gbrain_migrate_main

        gbrain_migrate_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["reconcile"]:
        from .reconcile import main as reconcile_main

        reconcile_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["bootstrap"]:
        from .bootstrap import main as bootstrap_main

        bootstrap_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["device-keygen"]:
        from .device_key import main as device_key_main

        device_key_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["sidecar-keygen"]:
        from .sidecar_key import main as sidecar_key_main

        sidecar_key_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["device-pair"]:
        from .device_pair import main as device_pair_main

        device_pair_main(sys.argv[2:])
        return
    if sys.argv[1:2] in (
        ["pairing-code"],
        ["bind-workspace"],
        ["revoke-device"],
        ["revoke-agent"],
        ["bootstrap-credential"],
    ):
        from .identity_cli import main as identity_main

        identity_main(sys.argv[1:])
        return

    parser = argparse.ArgumentParser(description="启动多 Agent 共享记忆 Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--principals-file", help="仅 SQLite 原型使用：保存固定 token hash 的受保护 JSON 文件")
    parser.add_argument("--metadata-dsn", default=os.environ.get("MEMORY_METADATA_RUNTIME_DSN"))
    parser.add_argument("--gbrain-dsn", default=os.environ.get("MEMORY_GBRAIN_BACKEND_DSN"))
    args = parser.parse_args()

    if bool(args.metadata_dsn) != bool(args.gbrain_dsn):
        parser.error("正式 Gateway 必须同时提供元数据库和 GBrain 后端连接串")

    GatewayHandler.db_path = Path(args.db)
    GatewayHandler.event_ledger = None
    GatewayHandler.query_service = None
    GatewayHandler.review_service = None
    GatewayHandler.crystal_service = None
    GatewayHandler.identity_service = None
    GatewayHandler.sync_service = None
    GatewayHandler.admin_service = None
    GatewayHandler.readiness_probe = None
    if args.metadata_dsn and args.gbrain_dsn:
        metadata_pool = PostgresConnectionPool.from_environment(
            args.metadata_dsn,
            name="memory-gateway-metadata",
            environment_prefix="MEMORY_METADATA_POOL",
            default_max_size=8,
        )
        gbrain_pool = PostgresConnectionPool.from_environment(
            args.gbrain_dsn,
            name="memory-gateway-gbrain",
            environment_prefix="MEMORY_GBRAIN_POOL",
            default_max_size=4,
        )
        metadata_pool.wait()
        gbrain_pool.wait()
        try:
            metadata_report = inspect_metadata_schema(args.metadata_dsn)
        except MigrationError as exc:
            parser.error(str(exc))
        if not metadata_report.compatible:
            parser.error("元数据库迁移不完整或校验值不一致，请先执行 memory-gateway migrate --verify")
        signer = AccessTokenSigner.from_environment()
        replay_cipher = RefreshReplayCipher.from_environment()
        security_scanner = SensitiveContentScanner.from_environment()
        GatewayHandler.authenticator = PostgresTokenAuthenticator(
            args.metadata_dsn, signer, connection_factory=metadata_pool.connection
        )
        GatewayHandler.identity_service = PostgresIdentityService(
            args.metadata_dsn,
            signer,
            replay_cipher,
            connection_factory=metadata_pool.connection,
        )
        cipher = EventCipher.from_environment()
        GatewayHandler.event_ledger = PostgresEventLedger(
            args.metadata_dsn,
            cipher,
            security_scanner,
            connection_factory=metadata_pool.connection,
        )
        GatewayHandler.sync_service = PostgresSyncService(
            args.metadata_dsn,
            GatewayHandler.event_ledger,
            cipher,
            connection_factory=metadata_pool.connection,
        )
        gbrain = GBrainBackend(args.gbrain_dsn, connection_factory=gbrain_pool.connection)
        gbrain.schema_version()
        GatewayHandler.query_service = PostgresQueryService(
            args.metadata_dsn,
            gbrain,
            connection_factory=metadata_pool.connection,
        )
        GatewayHandler.review_service = PostgresReviewService(
            args.metadata_dsn,
            cipher,
            gbrain,
            security_scanner=security_scanner,
            connection_factory=metadata_pool.connection,
        )
        GatewayHandler.crystal_service = PostgresCrystalService(
            args.metadata_dsn,
            gbrain,
            connection_factory=metadata_pool.connection,
        )
        GatewayHandler.admin_service = PostgresAdminService(
            args.metadata_dsn,
            connection_factory=metadata_pool.connection,
        )

        max_heartbeat_age = float(os.environ.get("MEMORY_WORKER_HEARTBEAT_MAX_SECONDS", "30"))
        if not 1 <= max_heartbeat_age <= 3600:
            parser.error("MEMORY_WORKER_HEARTBEAT_MAX_SECONDS 必须在 1 到 3600 之间")

        def readiness_probe() -> None:
            report = inspect_metadata_schema(args.metadata_dsn)
            if not report.compatible:
                raise MigrationError("元数据库迁移不完整或校验值不一致")
            with metadata_pool.connection() as connection:
                connection.execute("SELECT 1").fetchone()
                row = connection.execute(
                    "SELECT state_value FROM gateway_state WHERE state_key = 'worker_heartbeat'"
                ).fetchone()
            if row is None:
                raise RuntimeError("worker heartbeat missing")
            heartbeat = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            if heartbeat.tzinfo is None:
                raise RuntimeError("worker heartbeat invalid")
            age_seconds = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds()
            if age_seconds < 0 or age_seconds > max_heartbeat_age:
                raise RuntimeError("worker heartbeat stale")
            gbrain.schema_version()

        GatewayHandler.readiness_probe = readiness_probe
    else:
        if not args.principals_file:
            parser.error("SQLite 原型模式需要 --principals-file")
        GatewayHandler.authenticator = TokenAuthenticator.from_file(args.principals_file)
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print(f"Memory Gateway listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if args.metadata_dsn and args.gbrain_dsn:
            gbrain_pool.close()
            metadata_pool.close()


if __name__ == "__main__":
    main()
