-- 订单对账批次：发起即在同一事务内执行并完成（in_progress -> completed，终态）；
-- 失败不留批次、不留部分结论。对账只核对与留痕，不改变订单、退款、结算单的状态与金额。
CREATE TABLE IF NOT EXISTS reconciliations(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  paid_cents INTEGER,
  pending_refund_cents INTEGER,
  effective_refund_cents INTEGER,
  effective_settlement_cents INTEGER,
  settled_balance_cents INTEGER,
  conclusion TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  PRIMARY KEY(tenant, batch_id)
);

CREATE INDEX IF NOT EXISTS idx_reconciliations_order ON reconciliations(tenant, order_id);

-- 同一订单同一时刻至多一个进行中的对账批次（部分唯一索引兜底）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_reconciliations_order_in_progress
  ON reconciliations(tenant, order_id) WHERE status='in_progress';
