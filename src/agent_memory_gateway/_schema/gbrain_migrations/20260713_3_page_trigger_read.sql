-- pages 搜索向量触发器会读取 timeline_entries。
-- 仅授予与 memory-gateway 页面关联记录的 SELECT，供触发器正常执行。

BEGIN;

GRANT SELECT ON TABLE timeline_entries TO memory_gbrain_backend;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'timeline_entries' AND policyname = 'memory_gateway_timeline_entries_select') THEN
    CREATE POLICY memory_gateway_timeline_entries_select ON timeline_entries
      FOR SELECT TO memory_gbrain_backend
      USING (
        EXISTS (
          SELECT 1 FROM pages
          WHERE pages.id = timeline_entries.page_id
            AND pages.source_id LIKE 'memory-gateway:%'
        )
      );
  END IF;
END
$$;

COMMIT;
