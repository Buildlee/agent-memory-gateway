-- 将 OpenClaw 作为正式 Agent 类型；不修改任何已有 Agent 或配对码。
BEGIN;

ALTER TABLE agent_installations
  DROP CONSTRAINT IF EXISTS agent_installations_agent_type_check;

ALTER TABLE agent_installations
  ADD CONSTRAINT agent_installations_agent_type_check
  CHECK (agent_type IN ('codex', 'hermes', 'openclaw', 'other'));

COMMIT;
