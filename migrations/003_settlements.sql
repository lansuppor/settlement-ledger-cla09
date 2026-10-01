-- 结算单：受理后为 pending，推进成功后为 effective（终态）。
CREATE TABLE IF NOT EXISTS settlements(
  tenant TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant, settlement_id)
);

CREATE INDEX IF NOT EXISTS idx_settlements_order ON settlements(tenant, order_id);

-- 结算单引用的退款单集合，按受理顺序保存。
-- 注意：同一张待处理退款允许同时被多张待处理结算单引用；只有推进是原子的，
-- 先推进成功的结算单把退款置为 effective，其余结算单推进时按引用冲突失败。
-- 因此这里只在（结算单, 退款）维度去重，“一张退款只被核销一次”由推进事务中
-- 对 refunds.status 的判定与 BEGIN IMMEDIATE 串行化共同保证。
CREATE TABLE IF NOT EXISTS settlement_refunds(
  tenant TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY(tenant, settlement_id, refund_id)
);

CREATE INDEX IF NOT EXISTS idx_settlement_refunds_refund
  ON settlement_refunds(tenant, refund_id);
