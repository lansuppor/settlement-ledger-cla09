-- 异常工单：把异常处理诉求登记成可跟踪的独立单据。
-- 状态机：pending（待处理）-> processing（处理中）-> resolved（已解决）-> closed（已关闭，终态）；
-- pending/processing 也可直接解决或关闭。所有状态翻转均在单事务内原子完成，
-- 不改变订单、退款、结算单与对账批次的任何状态、金额与结论。
CREATE TABLE IF NOT EXISTS tickets(
  tenant TEXT NOT NULL,
  ticket_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  issue TEXT NOT NULL,
  resolution_note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, ticket_id)
);

CREATE INDEX IF NOT EXISTS idx_tickets_order ON tickets(tenant, order_id);
