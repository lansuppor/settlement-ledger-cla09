-- 对账批次：发起时进入进行中（in_progress），核对完成随即进入已完成（completed）。
-- 对账只做核对与留痕，不写回订单、退款、结算单的任何状态与金额，因此不对相关表建外键。
CREATE TABLE IF NOT EXISTS reconciliation_batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,             -- in_progress | completed
  paid_cents INTEGER NOT NULL,
  pending_refunds_cents INTEGER NOT NULL,
  effective_refunds_cents INTEGER NOT NULL,
  effective_settlements_cents INTEGER NOT NULL,
  verified_balance_cents INTEGER NOT NULL,
  conclusion TEXT NOT NULL,         -- balanced | refund_total_exceeds_received
  created_at TEXT NOT NULL,
  completed_at TEXT,
  PRIMARY KEY(tenant, batch_id)
);

CREATE INDEX IF NOT EXISTS idx_reconciliations_order
  ON reconciliation_batches(tenant, order_id);

-- 同一订单同时只允许一个进行中批次；部分唯一索引兜底，跨租户天然隔离。
CREATE UNIQUE INDEX IF NOT EXISTS ux_reconciliations_inprogress_per_order
  ON reconciliation_batches(tenant, order_id) WHERE status = 'in_progress';
