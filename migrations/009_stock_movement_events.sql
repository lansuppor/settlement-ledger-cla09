-- 出入库单操作留痕：受理（accept）与撤销（reverse）实际生效时，
-- 在与单据写入同一个事务内原子追加一行；失败、被拒绝（404/409）与
-- 幂等重放均不追加。每张单据至多一条受理留痕、一条撤销留痕，
-- 受理恒在前（seq=1）、撤销在后（seq=2）；seq 在同一单据内从 1 起
-- 连续递增，一经写入不再变化。两行都保留受理时的方向与数量，
-- 撤销记录的 occurred_at 为撤销时刻。
CREATE TABLE IF NOT EXISTS stock_movement_events(
  tenant TEXT NOT NULL,
  movement_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  operation TEXT NOT NULL,
  status TEXT NOT NULL,
  direction TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  occurred_at TEXT NOT NULL,
  PRIMARY KEY(tenant, movement_id, seq)
);

-- 每张单据每种操作至多一条留痕：唯一约束兜底，防止任何路径重复追加。
CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_movement_events_op
  ON stock_movement_events(tenant, movement_id, operation);
