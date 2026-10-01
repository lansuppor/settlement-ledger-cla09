CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, refund_id)
);

CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(tenant, order_id);

CREATE TABLE IF NOT EXISTS idempotent_requests(
  tenant TEXT NOT NULL,
  operation TEXT NOT NULL,
  target_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  result_code TEXT NOT NULL,
  result_status INTEGER NOT NULL,
  result_detail TEXT NOT NULL,
  result_body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, operation, target_id, request_fingerprint)
);
