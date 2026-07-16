-- 为 Sidecar push/pull 保存独立授权 epoch、策略版本和连续设备序号。
-- 只增加列并初始化全局 sync_epoch，不删除或重写事件正文。

BEGIN;

ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS last_contiguous_event_seq BIGINT NOT NULL DEFAULT 0
    CHECK (last_contiguous_event_seq >= 0);

ALTER TABLE sync_checkpoints
  ADD COLUMN IF NOT EXISTS device_auth_epoch BIGINT NOT NULL DEFAULT 1
    CHECK (device_auth_epoch > 0),
  ADD COLUMN IF NOT EXISTS agent_auth_epoch BIGINT NOT NULL DEFAULT 1
    CHECK (agent_auth_epoch > 0),
  ADD COLUMN IF NOT EXISTS policy_version TEXT NOT NULL DEFAULT '2026-07-12.2';

INSERT INTO gateway_state (state_key, state_value)
VALUES ('sync_epoch', 'sync_' || replace(gen_random_uuid()::text, '-', ''))
ON CONFLICT (state_key) DO NOTHING;

COMMIT;
