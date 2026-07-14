-- 不为历史事件猜测 scope。存在旧事件时必须先人工完成 scope 映射。

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'gateway_events'
      AND column_name = 'scope'
  ) THEN
    IF EXISTS (SELECT 1 FROM gateway_events) THEN
      RAISE EXCEPTION 'gateway_events 已有记录，必须先人工映射 scope，拒绝默认扩权';
    END IF;
    ALTER TABLE gateway_events
      ADD COLUMN scope TEXT NOT NULL DEFAULT 'workspace';
    ALTER TABLE gateway_events
      ALTER COLUMN scope DROP DEFAULT;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'gateway_events'::regclass
      AND conname = 'gateway_events_scope_check'
  ) THEN
    ALTER TABLE gateway_events
      ADD CONSTRAINT gateway_events_scope_check
      CHECK (scope IN ('user', 'workspace', 'device', 'agent', 'private'));
  END IF;
END $$;

COMMIT;
