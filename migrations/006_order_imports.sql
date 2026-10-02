-- 订单批量导入任务：受理后逐行校验并受理订单，每行独立落库，可断点续跑。
-- 任务状态：in_progress（尚有未处理行，含服务中断残留）-> completed（全部行处理完毕，终态）。
-- 每行在单事务内判定：成功则同事务写入订单并把行置为 success；
-- 失败则只把行置为 failure 并记录原因；中断回滚后行仍为 pending，可续跑重判。
CREATE TABLE IF NOT EXISTS order_import_tasks(
  task_id TEXT PRIMARY KEY,
  csv_content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  total_rows INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

-- 行号按数据区从 1 起计；raw 保留该行原始 CSV 文本用于错误清单回放。
CREATE TABLE IF NOT EXISTS order_import_task_rows(
  task_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  raw TEXT NOT NULL,
  status TEXT NOT NULL,
  error_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(task_id, line_no),
  FOREIGN KEY(task_id) REFERENCES order_import_tasks(task_id)
);
