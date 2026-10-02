-- 出入库单状态：accepted（已受理，默认）/ reversed（已冲正，终态）。
-- 冲正单据保留原值与创建时间，仅状态翻转，数量汇总时计 0。
-- SQLite 的 ALTER TABLE 不支持 ADD COLUMN IF NOT EXISTS，重复执行迁移时
-- “duplicate column name” 由 app.store.db.migrate 识别并跳过，保证可重入。
ALTER TABLE stock_movements ADD COLUMN status TEXT NOT NULL DEFAULT 'accepted';
