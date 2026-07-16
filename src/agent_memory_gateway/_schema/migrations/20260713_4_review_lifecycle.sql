-- 第六阶段：审核、冲突、修订与补偿状态账本。
-- 只追加结构和历史记录；不删除或改写既有事实正文。

ALTER TABLE review_candidates
  ADD COLUMN IF NOT EXISTS last_operation_id TEXT;

CREATE TABLE IF NOT EXISTS review_operations (
  operation_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  review_id TEXT NOT NULL REFERENCES review_candidates(review_id),
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  action TEXT NOT NULL CHECK (action IN (
    'confirm', 'confirm_edit', 'retain_both', 'supersede', 'archive', 'reject', 'revert'
  )),
  expected_revision BIGINT NOT NULL CHECK (expected_revision > 0),
  result_code TEXT NOT NULL,
  backend_ref TEXT,
  target_ref TEXT,
  content_hash TEXT,
  compensates_operation_id TEXT REFERENCES review_operations(operation_id),
  result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_operations_review_created
  ON review_operations (review_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_lifecycle (
  backend_ref TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  scope TEXT NOT NULL CHECK (scope IN ('user', 'workspace', 'device', 'agent', 'private')),
  source_device_id TEXT NOT NULL REFERENCES devices(device_id),
  source_agent_installation_id TEXT NOT NULL
    REFERENCES agent_installations(agent_installation_id),
  source_event_id TEXT NOT NULL,
  review_id TEXT REFERENCES review_candidates(review_id),
  entity_key TEXT,
  attribute_key TEXT,
  temporal_key TEXT,
  namespace_key TEXT NOT NULL,
  evidence TEXT NOT NULL,
  confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  instruction_like BOOLEAN NOT NULL DEFAULT false,
  pinned BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'archived', 'pending_deletion')),
  superseded_by TEXT REFERENCES memory_lifecycle(backend_ref),
  created_server_revision BIGINT NOT NULL CHECK (created_server_revision > 0),
  updated_server_revision BIGINT NOT NULL CHECK (updated_server_revision > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (source_device_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_scope_state
  ON memory_lifecycle (
    tenant_id, user_id, workspace_id, scope, namespace_key, entity_key, attribute_key, temporal_key, status
  );
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_source
  ON memory_lifecycle (source_device_id, source_event_id);

CREATE TABLE IF NOT EXISTS memory_lifecycle_history (
  history_id TEXT PRIMARY KEY,
  backend_ref TEXT NOT NULL REFERENCES memory_lifecycle(backend_ref),
  operation_id TEXT REFERENCES review_operations(operation_id),
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  action TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  related_ref TEXT,
  server_revision BIGINT NOT NULL CHECK (server_revision > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_history_ref
  ON memory_lifecycle_history (backend_ref, server_revision DESC);

-- 让已落地的事实也进入生命周期视图；正文仍保持在原有加密事件/GBrain 中。
INSERT INTO memory_lifecycle (
  backend_ref, tenant_id, user_id, workspace_id, scope,
  source_device_id, source_agent_installation_id, source_event_id,
  evidence, confidence, instruction_like, namespace_key, status,
  created_server_revision, updated_server_revision
)
SELECT
  event.backend_ref, event.tenant_id, event.user_id, event.workspace_id, event.scope,
  event.device_id, event.agent_installation_id, event.event_id,
  'user_explicit', 1.0, event.instruction_like, 'device:' || event.device_id, 'active',
  COALESCE(event.server_revision, 1), COALESCE(event.server_revision, 1)
FROM gateway_events AS event
WHERE event.status = 'applied'
  AND event.backend_ref IS NOT NULL
ON CONFLICT (backend_ref) DO NOTHING;

INSERT INTO memory_lifecycle_history (
  history_id, backend_ref, tenant_id, user_id, action, to_status, server_revision
)
SELECT
  'hist_backfill_' || md5(lifecycle.backend_ref),
  lifecycle.backend_ref, lifecycle.tenant_id, lifecycle.user_id,
  'backfill', lifecycle.status, lifecycle.created_server_revision
FROM memory_lifecycle AS lifecycle
WHERE NOT EXISTS (
  SELECT 1 FROM memory_lifecycle_history AS history
  WHERE history.backend_ref = lifecycle.backend_ref
);
