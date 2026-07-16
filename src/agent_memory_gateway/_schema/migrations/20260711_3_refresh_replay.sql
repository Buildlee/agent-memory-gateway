-- 刷新凭据轮换后的短暂加密重放响应。
--
-- 只增加 nullable 密文字段；不猜测、不转换，也不读取历史凭据明文。

BEGIN;

LOCK TABLE refresh_credentials IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE refresh_credentials
  ADD COLUMN IF NOT EXISTS replacement_ciphertext BYTEA,
  ADD COLUMN IF NOT EXISTS replacement_nonce BYTEA,
  ADD COLUMN IF NOT EXISTS replacement_key_version TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'refresh_credentials_replacement_ciphertext_complete'
      AND conrelid = 'refresh_credentials'::regclass
  ) THEN
    ALTER TABLE refresh_credentials
      ADD CONSTRAINT refresh_credentials_replacement_ciphertext_complete CHECK (
        (replacement_ciphertext IS NULL AND replacement_nonce IS NULL AND replacement_key_version IS NULL)
        OR
        (replacement_ciphertext IS NOT NULL AND replacement_nonce IS NOT NULL AND replacement_key_version IS NOT NULL)
      );
  END IF;
END
$$;

COMMIT;
