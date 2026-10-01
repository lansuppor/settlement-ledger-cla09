CREATE TABLE IF NOT EXISTS refunds(
  tenant TEXT NOT NULL,
  refund_id TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, refund_id)
);

-- 幂等重放记录：同一（租户, 操作, 目标标识, 请求指纹）只生效一次。
-- operation: 'refund.accept' | 'refund.reverse'
-- target_id: 受理为 refund_id，冲正为 refund_id
CREATE TABLE IF NOT EXISTS idempotent_requests(
  tenant TEXT NOT NULL,
  operation TEXT NOT NULL,
  target_id TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  response_code INTEGER NOT NULL,
  response_body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, operation, target_id, request_fingerprint)
);
