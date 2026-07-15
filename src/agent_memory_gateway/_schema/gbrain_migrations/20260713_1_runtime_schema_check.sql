-- 允许 GBrainBackend 在启动时读取 adapter 版本和校验值。
-- 只补充迁移元数据的 SELECT，不授予任何数据修改或删除权限。

BEGIN;

GRANT SELECT ON TABLE memory_gateway_adapter_migrations
  TO memory_gbrain_backend;

COMMIT;
