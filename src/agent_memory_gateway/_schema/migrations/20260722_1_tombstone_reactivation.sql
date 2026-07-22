BEGIN;

-- 回滚 supersede 时保留墓碑审计记录，但停止把它下发给 Sidecar。
ALTER TABLE memory_tombstones
  ADD COLUMN IF NOT EXISTS revoked_revision BIGINT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory_tombstones'::regclass
      AND conname = 'memory_tombstones_revoked_revision_check'
  ) THEN
    ALTER TABLE memory_tombstones
      ADD CONSTRAINT memory_tombstones_revoked_revision_check
      CHECK (revoked_revision IS NULL OR revoked_revision > deleted_revision);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memory_tombstones_active_owner_revision
  ON memory_tombstones (tenant_id, user_id, deleted_revision DESC)
  WHERE revoked_revision IS NULL;

COMMIT;
