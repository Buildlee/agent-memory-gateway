-- 扩展设备类型，使 Linux 与 macOS 客户端无需伪装成 other；不修改任何已有设备记录。
BEGIN;

ALTER TABLE devices
  DROP CONSTRAINT IF EXISTS devices_device_type_check;

ALTER TABLE devices
  ADD CONSTRAINT devices_device_type_check
  CHECK (device_type IN ('windows', 'linux', 'macos', 'nas', 'other'));

ALTER TABLE pairing_codes
  DROP CONSTRAINT IF EXISTS pairing_codes_allowed_device_type_check;

ALTER TABLE pairing_codes
  ADD CONSTRAINT pairing_codes_allowed_device_type_check
  CHECK (allowed_device_type IN ('windows', 'linux', 'macos', 'nas', 'other'));

COMMIT;
