-- 出入库单：把订单维度的入库/出库数量登记成独立单据。
-- 方向仅 in（入库）/ out（出库），数量为正整数；受理后方向与数量保留原值。
-- 出入库单只作用于自身，不改变订单、退款、结算单、对账批次、工单与导入任务的任何状态与金额。
CREATE TABLE IF NOT EXISTS stock_movements(
  tenant TEXT NOT NULL,
  movement_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  direction TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, movement_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_movements_order ON stock_movements(tenant, order_id);
-- 检索按创建时间升序、相同按标识升序稳定排序，游标分页沿该索引推进。
CREATE INDEX IF NOT EXISTS idx_stock_movements_created ON stock_movements(tenant, created_at, movement_id);
