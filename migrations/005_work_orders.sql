-- 工单：受理时为 pending（待处理），状态机为
-- pending -> in_progress -> resolved -> closed（亦允许 pending/in_progress 直接关闭，
-- resolved 可回到处理中等业务侧由接口状态机约束，表本身只持久化状态串）。
-- 工单的登记与状态翻转不改变订单、退款、结算单与对账批次的任何状态、金额与结论。
CREATE TABLE IF NOT EXISTS work_orders(
  tenant TEXT NOT NULL,
  work_order_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  issue TEXT NOT NULL DEFAULT '',
  resolution TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, work_order_id)
);

CREATE INDEX IF NOT EXISTS idx_work_orders_order ON work_orders(tenant, order_id);
