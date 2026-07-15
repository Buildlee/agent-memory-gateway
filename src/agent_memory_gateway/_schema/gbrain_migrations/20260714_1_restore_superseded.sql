-- 允许审核补偿操作记录“恢复被取代事实”；不删除任何业务数据。

ALTER TABLE memory_gateway_operations
  DROP CONSTRAINT IF EXISTS memory_gateway_operations_operation_check;

ALTER TABLE memory_gateway_operations
  ADD CONSTRAINT memory_gateway_operations_operation_check
  CHECK (operation IN (
    'supersede', 'archive', 'reactivate', 'tombstone', 'rebuild_crystal', 'restore_superseded'
  ));
