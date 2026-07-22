CREATE TABLE IF NOT EXISTS memory_recall_events (
  recall_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  agent_installation_id TEXT NOT NULL REFERENCES agent_installations(agent_installation_id),
  query_hash CHAR(64) NOT NULL,
  memory_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  item_count INTEGER NOT NULL CHECK (item_count >= 0 AND item_count <= 50),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT memory_recall_events_refs_array CHECK (jsonb_typeof(memory_refs) = 'array')
);

CREATE INDEX IF NOT EXISTS idx_memory_recall_events_workspace_created
  ON memory_recall_events (tenant_id, user_id, workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_feedback_events (
  feedback_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  agent_installation_id TEXT NOT NULL REFERENCES agent_installations(agent_installation_id),
  memory_id TEXT NOT NULL,
  recall_id TEXT REFERENCES memory_recall_events(recall_id),
  action TEXT NOT NULL CHECK (action IN ('useful', 'pin', 'outdated', 'incorrect')),
  idempotency_key TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, user_id, agent_installation_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_memory_feedback_events_memory_created
  ON memory_feedback_events (tenant_id, user_id, workspace_id, memory_id, created_at DESC);

CREATE TABLE IF NOT EXISTS external_memory_bindings (
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  user_id TEXT NOT NULL REFERENCES users(user_id),
  device_id TEXT NOT NULL REFERENCES devices(device_id),
  agent_installation_id TEXT NOT NULL REFERENCES agent_installations(agent_installation_id),
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  provider_type TEXT NOT NULL,
  provider_instance_id TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  source_revision CHAR(64) NOT NULL,
  capture_mode TEXT NOT NULL CHECK (capture_mode IN ('manual_selection', 'automatic_whitelist')),
  event_id TEXT NOT NULL,
  backend_ref TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_id, workspace_id, provider_instance_id, source_record_id, source_revision),
  UNIQUE (device_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_external_memory_bindings_workspace
  ON external_memory_bindings (tenant_id, user_id, workspace_id, created_at DESC);
