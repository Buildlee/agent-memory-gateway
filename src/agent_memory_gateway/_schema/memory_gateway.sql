-- Memory Gateway 元数据库基线。
--
-- 仅在 `memory_gateway` 数据库中执行。本脚本不创建数据库、角色或扩展，
-- 不接触 `gbrain` 数据库，也不会在 Gateway 启动时自动执行。
-- 通过受控迁移命令显式执行前，必须先完成备份和 schema 检查。

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
  tenant_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'disabled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'disabled')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);

CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  display_name TEXT NOT NULL,
  device_type TEXT NOT NULL CHECK (device_type IN ('windows', 'nas', 'other')),
  public_key TEXT NOT NULL,
  auth_epoch BIGINT NOT NULL DEFAULT 1 CHECK (auth_epoch > 0),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('pending', 'active', 'revoked', 'disabled')),
  paired_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ,
  last_seen_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id, display_name)
);

CREATE INDEX IF NOT EXISTS idx_devices_principal
  ON devices (tenant_id, user_id, status);

CREATE TABLE IF NOT EXISTS agent_installations (
  agent_installation_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  agent_type TEXT NOT NULL CHECK (agent_type IN ('codex', 'hermes', 'other')),
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('pending', 'active', 'revoked', 'disabled')),
  auth_epoch BIGINT NOT NULL DEFAULT 1 CHECK (auth_epoch > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (device_id, display_name)
);

CREATE INDEX IF NOT EXISTS idx_agent_installations_device
  ON agent_installations (device_id, status);

CREATE TABLE IF NOT EXISTS workspaces (
  workspace_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  display_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'archived')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id, display_name)
);

CREATE TABLE IF NOT EXISTS workspace_bindings (
  agent_installation_id TEXT NOT NULL
    REFERENCES agent_installations(agent_installation_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  capabilities TEXT[] NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_installation_id, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_bindings_workspace
  ON workspace_bindings (workspace_id, status);

CREATE TABLE IF NOT EXISTS pairing_codes (
  pairing_code_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  code_hash TEXT NOT NULL UNIQUE,
  allowed_device_type TEXT NOT NULL CHECK (allowed_device_type IN ('windows', 'nas', 'other')),
  allowed_agent_types TEXT[] NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ,
  used_by_device_id TEXT REFERENCES devices(device_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (used_at IS NULL OR used_at <= expires_at)
);

CREATE INDEX IF NOT EXISTS idx_pairing_codes_active
  ON pairing_codes (expires_at)
  WHERE used_at IS NULL;

CREATE TABLE IF NOT EXISTS refresh_credentials (
  credential_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  credential_hash TEXT NOT NULL UNIQUE,
  auth_epoch BIGINT NOT NULL CHECK (auth_epoch > 0),
  previous_credential_hash TEXT,
  replay_until TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  CHECK (replay_until IS NULL OR replay_until <= expires_at)
);

CREATE INDEX IF NOT EXISTS idx_refresh_credentials_device
  ON refresh_credentials (device_id, expires_at)
  WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS gateway_events (
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  event_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  agent_installation_id TEXT NOT NULL
    REFERENCES agent_installations(agent_installation_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  session_id TEXT,
  device_seq BIGINT NOT NULL CHECK (device_seq >= 0),
  event_type TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK (schema_version > 0),
  causation_id TEXT,
  payload_hash TEXT NOT NULL,
  payload_ciphertext BYTEA,
  payload_nonce BYTEA,
  payload_key_version TEXT,
  status TEXT NOT NULL
    CHECK (status IN ('pending', 'applied', 'rejected', 'retryable_failed', 'dead_letter')),
  result_code TEXT,
  error_code TEXT,
  error_retryable BOOLEAN,
  backend_ref TEXT,
  server_revision BIGINT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  next_retry_at TIMESTAMPTZ,
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  PRIMARY KEY (device_id, event_id),
  UNIQUE (device_id, device_seq),
  UNIQUE (server_revision),
  CHECK (
    (payload_ciphertext IS NULL AND payload_nonce IS NULL AND payload_key_version IS NULL)
    OR (payload_ciphertext IS NOT NULL AND payload_nonce IS NOT NULL AND payload_key_version IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_gateway_events_pending
  ON gateway_events (next_retry_at, received_at)
  WHERE status IN ('pending', 'retryable_failed');
CREATE INDEX IF NOT EXISTS idx_gateway_events_workspace_revision
  ON gateway_events (tenant_id, user_id, workspace_id, server_revision DESC);

CREATE TABLE IF NOT EXISTS event_receipts (
  device_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  ack_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('applied', 'rejected')),
  result_code TEXT,
  error_code TEXT,
  backend_ref TEXT,
  server_revision BIGINT,
  trace_id TEXT NOT NULL,
  processed_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (device_id, event_id),
  FOREIGN KEY (device_id, event_id)
    REFERENCES gateway_events(device_id, event_id)
);

CREATE TABLE IF NOT EXISTS backend_bindings (
  idempotency_key TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  backend_name TEXT NOT NULL DEFAULT 'gbrain',
  backend_ref TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (backend_name, backend_ref),
  FOREIGN KEY (device_id, event_id)
    REFERENCES gateway_events(device_id, event_id)
);

CREATE TABLE IF NOT EXISTS review_candidates (
  review_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  candidate_ciphertext BYTEA NOT NULL,
  candidate_nonce BYTEA NOT NULL,
  candidate_key_version TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('pending', 'confirmed', 'rejected', 'archived', 'expired')),
  revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
  expires_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ,
  resolved_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (device_id, event_id)
    REFERENCES gateway_events(device_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_review_candidates_pending
  ON review_candidates (created_at)
  WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS memory_tombstones (
  memory_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  backend_ref TEXT NOT NULL,
  deleted_revision BIGINT NOT NULL UNIQUE CHECK (deleted_revision > 0),
  deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_by_device_id TEXT REFERENCES devices(device_id),
  reason_code TEXT NOT NULL,
  revoked_revision BIGINT,
    CONSTRAINT memory_tombstones_revoked_revision_check
      CHECK (revoked_revision IS NULL OR revoked_revision > deleted_revision),
  UNIQUE (backend_ref)
);

CREATE INDEX IF NOT EXISTS idx_memory_tombstones_owner_revision
  ON memory_tombstones (tenant_id, user_id, deleted_revision DESC);
CREATE INDEX IF NOT EXISTS idx_memory_tombstones_active_owner_revision
  ON memory_tombstones (tenant_id, user_id, deleted_revision DESC)
  WHERE revoked_revision IS NULL;

CREATE TABLE IF NOT EXISTS sync_checkpoints (
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  agent_installation_id TEXT NOT NULL
    REFERENCES agent_installations(agent_installation_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  server_revision BIGINT NOT NULL DEFAULT 0 CHECK (server_revision >= 0),
  sync_epoch TEXT NOT NULL,
  auth_epoch BIGINT NOT NULL CHECK (auth_epoch > 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (device_id, agent_installation_id, workspace_id)
);

CREATE TABLE IF NOT EXISTS dead_letters (
  dead_letter_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  error_code TEXT NOT NULL,
  last_error_class TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ,
  resolution_code TEXT,
  UNIQUE (device_id, event_id),
  FOREIGN KEY (device_id, event_id)
    REFERENCES gateway_events(device_id, event_id)
);

CREATE TABLE IF NOT EXISTS gateway_state (
  state_key TEXT PRIMARY KEY,
  state_value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('device', 'admin', 'system')),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  result_code TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  device_id TEXT,
  agent_installation_id TEXT,
  workspace_id TEXT,
  target_ref TEXT,
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_created
  ON audit_log (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_trace
  ON audit_log (trace_id);

-- Gateway 运行账号不拥有 schema，也不具有 DELETE 权限。清理候选正文、
-- 过期令牌和审计保留期通过受控 UPDATE 或专门的维护流程完成。
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public
  TO memory_gateway_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
  TO memory_gateway_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO memory_gateway_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO memory_gateway_runtime;

COMMIT;
