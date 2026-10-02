-- 出入库单冲正：状态生命周期 accepted -> reversed（终态）。
-- reversed 单据不计入数量汇总；既有单据一律视为 accepted。
ALTER TABLE stock_movements ADD COLUMN status TEXT NOT NULL DEFAULT 'accepted';
