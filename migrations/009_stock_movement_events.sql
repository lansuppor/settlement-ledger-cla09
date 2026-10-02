-- 出入库单操作留痕：受理（accept）与撤销（reverse）在生效的同一事务内原子写入。
-- 序号 seq 在同一单据内从 1 起连续递增、一经写入不再变化；撤销记录保留受理时的
-- 方向与数量。留痕只读，查询不改变任何单据、任务与订单数据。
CREATE TABLE IF NOT EXISTS stock_movement_events(
  tenant TEXT NOT NULL,
  movement_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL,
  direction TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  occurred_at TEXT NOT NULL,
  PRIMARY KEY(tenant, movement_id, seq)
);
