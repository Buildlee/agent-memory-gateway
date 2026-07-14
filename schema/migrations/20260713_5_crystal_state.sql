-- 第六阶段结晶状态：只登记页面引用、来源引用和失效状态，不复制正文。

ALTER TABLE memory_lifecycle
  ADD COLUMN IF NOT EXISTS scope_binding_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_crystal_scope
  ON memory_lifecycle (scope_binding_hash, status)
  WHERE scope_binding_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_crystals (
  scope_binding_hash TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  scope TEXT NOT NULL CHECK (scope IN ('user', 'workspace', 'device', 'agent', 'private')),
  namespace_key TEXT NOT NULL,
  page_ref TEXT,
  source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  rule_version TEXT NOT NULL DEFAULT 'crystal-v1',
  status TEXT NOT NULL CHECK (status IN ('ready', 'stale', 'failed')),
  generated_server_revision BIGINT,
  stale_server_revision BIGINT,
  last_error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_crystals_scope_state
  ON memory_crystals (tenant_id, user_id, workspace_id, scope, namespace_key, status);
