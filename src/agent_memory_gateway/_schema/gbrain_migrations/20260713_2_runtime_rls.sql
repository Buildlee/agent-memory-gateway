-- 为最小权限 GBrainBackend 账号增加显式行级安全策略。
-- 只允许访问 memory-gateway:* 来源；不授予 DELETE 或 TRUNCATE。

BEGIN;

ALTER TABLE memory_gateway_bindings
  ADD COLUMN IF NOT EXISTS source_id TEXT;

UPDATE memory_gateway_bindings AS binding
SET source_id = fact.source_id
FROM facts AS fact
WHERE binding.fact_id = fact.id
  AND binding.source_id IS NULL;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM memory_gateway_bindings WHERE source_id IS NULL) THEN
    RAISE EXCEPTION 'cannot derive source_id for memory_gateway_bindings';
  END IF;
END
$$;

ALTER TABLE memory_gateway_bindings
  ALTER COLUMN source_id SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory_gateway_bindings'::regclass
      AND conname = 'memory_gateway_bindings_source_id_fkey'
  ) THEN
    ALTER TABLE memory_gateway_bindings
      ADD CONSTRAINT memory_gateway_bindings_source_id_fkey
      FOREIGN KEY (source_id) REFERENCES sources(id);
  END IF;
END
$$;

ALTER TABLE memory_gateway_operations
  ADD COLUMN IF NOT EXISTS source_id TEXT;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM memory_gateway_operations WHERE source_id IS NULL) THEN
    RAISE EXCEPTION 'existing lifecycle operations require explicit source_id migration';
  END IF;
END
$$;

ALTER TABLE memory_gateway_operations
  ALTER COLUMN source_id SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'memory_gateway_operations'::regclass
      AND conname = 'memory_gateway_operations_source_id_fkey'
  ) THEN
    ALTER TABLE memory_gateway_operations
      ADD CONSTRAINT memory_gateway_operations_source_id_fkey
      FOREIGN KEY (source_id) REFERENCES sources(id);
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'memory_gateway_adapter_migrations' AND policyname = 'memory_gateway_migrations_select') THEN
    CREATE POLICY memory_gateway_migrations_select ON memory_gateway_adapter_migrations FOR SELECT TO memory_gbrain_backend USING (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'sources' AND policyname = 'memory_gateway_sources_select') THEN
    CREATE POLICY memory_gateway_sources_select ON sources FOR SELECT TO memory_gbrain_backend USING (id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'sources' AND policyname = 'memory_gateway_sources_insert') THEN
    CREATE POLICY memory_gateway_sources_insert ON sources FOR INSERT TO memory_gbrain_backend WITH CHECK (id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'facts' AND policyname = 'memory_gateway_facts_select') THEN
    CREATE POLICY memory_gateway_facts_select ON facts FOR SELECT TO memory_gbrain_backend USING (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'facts' AND policyname = 'memory_gateway_facts_insert') THEN
    CREATE POLICY memory_gateway_facts_insert ON facts FOR INSERT TO memory_gbrain_backend WITH CHECK (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'facts' AND policyname = 'memory_gateway_facts_update') THEN
    CREATE POLICY memory_gateway_facts_update ON facts FOR UPDATE TO memory_gbrain_backend USING (source_id LIKE 'memory-gateway:%') WITH CHECK (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'pages' AND policyname = 'memory_gateway_pages_select') THEN
    CREATE POLICY memory_gateway_pages_select ON pages FOR SELECT TO memory_gbrain_backend USING (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'pages' AND policyname = 'memory_gateway_pages_insert') THEN
    CREATE POLICY memory_gateway_pages_insert ON pages FOR INSERT TO memory_gbrain_backend WITH CHECK (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'pages' AND policyname = 'memory_gateway_pages_update') THEN
    CREATE POLICY memory_gateway_pages_update ON pages FOR UPDATE TO memory_gbrain_backend USING (source_id LIKE 'memory-gateway:%') WITH CHECK (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'memory_gateway_bindings' AND policyname = 'memory_gateway_bindings_select') THEN
    CREATE POLICY memory_gateway_bindings_select ON memory_gateway_bindings FOR SELECT TO memory_gbrain_backend USING (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'memory_gateway_bindings' AND policyname = 'memory_gateway_bindings_insert') THEN
    CREATE POLICY memory_gateway_bindings_insert ON memory_gateway_bindings FOR INSERT TO memory_gbrain_backend WITH CHECK (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'memory_gateway_operations' AND policyname = 'memory_gateway_operations_select') THEN
    CREATE POLICY memory_gateway_operations_select ON memory_gateway_operations FOR SELECT TO memory_gbrain_backend USING (source_id LIKE 'memory-gateway:%');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'memory_gateway_operations' AND policyname = 'memory_gateway_operations_insert') THEN
    CREATE POLICY memory_gateway_operations_insert ON memory_gateway_operations FOR INSERT TO memory_gbrain_backend WITH CHECK (source_id LIKE 'memory-gateway:%');
  END IF;
END
$$;

COMMIT;
