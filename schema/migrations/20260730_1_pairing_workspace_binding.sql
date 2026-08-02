-- 一次性配对码可携带由管理员预先限定的工作区和能力；客户端不能自行扩大授权。
BEGIN;

ALTER TABLE pairing_codes
  ADD COLUMN IF NOT EXISTS workspace_id TEXT REFERENCES workspaces(workspace_id);

ALTER TABLE pairing_codes
  ADD COLUMN IF NOT EXISTS workspace_capabilities TEXT[];

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'pairing_codes'::regclass
      AND conname = 'pairing_codes_workspace_binding_check'
  ) THEN
    ALTER TABLE pairing_codes
      ADD CONSTRAINT pairing_codes_workspace_binding_check
      CHECK (
        (workspace_id IS NULL AND workspace_capabilities IS NULL)
        OR (
          workspace_id IS NOT NULL
          AND cardinality(workspace_capabilities) > 0
        )
      );
  END IF;
END $$;

COMMIT;
