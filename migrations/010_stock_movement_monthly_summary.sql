-- 月度数量汇总支撑：按 (租户, 订单标识, 创建时间) 组织索引，
-- 覆盖按订单 + 创建时间年月前缀的 GROUP BY 与 (order_id, month) 升序游标分页。
-- 月归属取单据创建时间（UTC ISO）前 7 位 YYYY-MM，受理时固定、冲正不改创建时间。
CREATE INDEX IF NOT EXISTS idx_stock_movements_monthly
  ON stock_movements(tenant, order_id, created_at);
