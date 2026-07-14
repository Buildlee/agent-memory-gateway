-- GBrain 适配器的最小幂等绑定表。
-- 仅在现有 gbrain 数据库中由显式迁移执行；不修改已有 GBrain 表定义或数据。

BEGIN;

CREATE TABLE IF NOT EXISTS memory_gateway_adapter_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_gateway_bindings (
  idempotency_key TEXT PRIMARY KEY,
  backend_ref TEXT NOT NULL UNIQUE,
  fact_id BIGINT NOT NULL UNIQUE REFERENCES facts(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON TABLE sources, facts, memory_gateway_bindings
  TO memory_gbrain_backend;
GRANT USAGE, SELECT ON SEQUENCE facts_id_seq
  TO memory_gbrain_backend;

COMMIT;
