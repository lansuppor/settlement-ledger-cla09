-- 出入库单：对已受理订单发生的入库（in）/出库（out）记录，独立单据。
-- 受理后方向与数量保持原值、不可变；出入库单只作用于自身，不改变订单、
-- 退款、结算单、对账批次、工单与批量导入任务的状态、金额与结论。
CREATE TABLE IF NOT EXISTS stock_movements(
  tenant TEXT NOT NULL,
  movement_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, movement_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_movements_order
  ON stock_movements(tenant, order_id);

-- 稳定排序支撑：按创建时间升序、相同则按标识升序的游标分页。
CREATE INDEX IF NOT EXISTS idx_stock_movements_sort
  ON stock_movements(tenant, created_at, movement_id);
