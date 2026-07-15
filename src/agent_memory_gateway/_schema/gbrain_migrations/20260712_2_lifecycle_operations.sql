-- 为 GBrainBackend 生命周期写操作增加不含正文的幂等操作账本。
-- 只授予明确列的 UPDATE，不授予 DELETE、TRUNCATE 或 schema CREATE。

BEGIN;

CREATE TABLE IF NOT EXISTS memory_gateway_operations (
  idempotency_key TEXT PRIMARY KEY,
  operation TEXT NOT NULL CHECK (
    operation IN ('supersede', 'archive', 'reactivate', 'tombstone', 'rebuild_crystal')
  ),
  target_ref TEXT NOT NULL,
  result_ref TEXT,
  deleted_revision BIGINT CHECK (deleted_revision IS NULL OR deleted_revision > 0),
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON TABLE memory_gateway_operations
  TO memory_gbrain_backend;

GRANT UPDATE (superseded_by, valid_until, expired_at, consolidated_at, consolidated_into)
  ON TABLE facts TO memory_gbrain_backend;

GRANT SELECT, INSERT ON TABLE pages TO memory_gbrain_backend;
GRANT UPDATE (compiled_truth, frontmatter, content_hash, updated_at, deleted_at)
  ON TABLE pages TO memory_gbrain_backend;
GRANT USAGE, SELECT ON SEQUENCE pages_id_seq TO memory_gbrain_backend;

COMMIT;
