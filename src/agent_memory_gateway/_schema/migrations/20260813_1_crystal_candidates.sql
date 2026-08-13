-- 结晶后台候选：只保存范围、来源引用和修订号，不保存记忆正文。

CREATE TABLE IF NOT EXISTS crystal_rebuild_candidates (
  candidate_id TEXT PRIMARY KEY,
  scope_binding_hash TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  scope TEXT NOT NULL CHECK (scope IN ('user', 'workspace', 'device', 'agent', 'private')),
  namespace_key TEXT NOT NULL,
  source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_count INTEGER NOT NULL CHECK (source_count >= 2),
  source_revision BIGINT NOT NULL CHECK (source_revision > 0),
  reason TEXT NOT NULL CHECK (reason IN ('missing', 'stale', 'source_changed')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'rebuilt', 'dismissed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT crystal_rebuild_candidates_refs_array CHECK (jsonb_typeof(source_refs) = 'array')
);

CREATE INDEX IF NOT EXISTS idx_crystal_rebuild_candidates_workspace_status
  ON crystal_rebuild_candidates (tenant_id, user_id, workspace_id, status, updated_at DESC);
