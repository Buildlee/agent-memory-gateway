-- 为普通检索增加 fail-closed 的命令式内容隔离标记。
--
-- 历史事件无法安全推断，统一按 true 隔离；新事件必须由 Gateway 明确写入分类结果。

BEGIN;

LOCK TABLE gateway_events IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE gateway_events
  ADD COLUMN IF NOT EXISTS instruction_like BOOLEAN NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS idx_gateway_events_visible_references
  ON gateway_events (tenant_id, user_id, workspace_id, server_revision DESC)
  WHERE status = 'applied' AND backend_ref IS NOT NULL AND instruction_like = false;

COMMIT;
