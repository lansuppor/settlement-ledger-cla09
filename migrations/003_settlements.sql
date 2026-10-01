-- 结算单：受理时为 pending，推进成功后为 effective（终态）。
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

-- 结算单引用的退款单集合（受理时快照；引用的退款当时可能尚不存在，
-- “引用的退款单不存在”是推进期才给出的失败原因，故不对 refunds 建外键）。
CREATE TABLE IF NOT EXISTS settlement_refunds(
  tenant TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY(tenant, settlement_id, refund_id),
  UNIQUE(tenant, settlement_id, position),
  FOREIGN KEY(tenant, settlement_id) REFERENCES settlements(tenant, settlement_id)
);

-- 每张退款单至多被一张结算单核销：仅推进成功的结算单写入声明。
CREATE TABLE IF NOT EXISTS settlement_claims(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, refund_id),
  FOREIGN KEY(tenant, settlement_id) REFERENCES settlements(tenant, settlement_id),
  FOREIGN KEY(tenant, refund_id) REFERENCES refunds(tenant, refund_id)
);
