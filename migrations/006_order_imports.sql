-- 订单批量导入任务：提交即创建并逐行校验受理，失败的行只记录错误原因。
-- 每行在独立事务内处理（成功插订单或记录失败行），故整批执行中断时
-- 不会留下“半行”——行只在已决（成功受理或已判失败）后才标记 processed。
-- 任务只有 pending（未完成，可续跑）与 completed（已完成，终态）两态。
-- 同一任务标识以内容指纹去重：同内容重放返回首次结果；不同内容冲突（409）。
CREATE TABLE IF NOT EXISTS import_tasks(
  tenant TEXT NOT NULL,
  task_id TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  total_rows INTEGER NOT NULL,
  success_count INTEGER NOT NULL,
  failure_count INTEGER NOT NULL,
  processed_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  PRIMARY KEY(tenant, task_id)
);

-- 每行原始内容与判定结果：pending=尚未处理，accepted=已受理为订单，
-- failed=已判失败（error_reason 给出原因，仅 failed 行非空）。
CREATE TABLE IF NOT EXISTS import_task_rows(
  tenant TEXT NOT NULL,
  task_id TEXT NOT NULL,
  line_number INTEGER NOT NULL,
  tenant_value TEXT NOT NULL,
  order_id_value TEXT NOT NULL,
  amount_text TEXT NOT NULL,
  currency_value TEXT NOT NULL,
  raw_line TEXT NOT NULL,
  status TEXT NOT NULL,
  error_reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(tenant, task_id, line_number),
  FOREIGN KEY(tenant, task_id) REFERENCES import_tasks(tenant, task_id)
);
