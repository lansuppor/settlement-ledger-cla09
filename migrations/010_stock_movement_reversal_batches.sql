-- 出入库单一次性批量冲正批次记录：以（租户, 批次标识）持久识别整次请求。
-- 首次合法请求把规范化（去重、按标识排序）后的单据标识集合与首次业务结果
-- （含 200 成功体与 404/409 首次错误）一并原子落库；同批次同集合重放
-- （排列顺序变化、服务重启、单据状态后来变化）一律返回首次结果。
-- 同批次不同集合不写入、不覆盖，冲突在业务层判定。批次标识独立于单据标识，
-- 不写入 idempotent_requests，不影响单张冲正的幂等记录。
-- 仅“写入选争用导致无法开始处理”的请求不会到达本表，故批次标识不被占用。
CREATE TABLE IF NOT EXISTS stock_movement_reversal_batches(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  movement_ids TEXT NOT NULL,
  result_code TEXT NOT NULL,
  result_status INTEGER NOT NULL,
  result_detail TEXT NOT NULL,
  result_body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant, batch_id)
);
