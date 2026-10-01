# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；退款单的受理、读取、冲正与幂等重放；结算单的受理、读取、推进（把引用的待处理退款原子推进为已生效）与撤销（解除已生效结算单的全部核销，退款回到待处理）；订单对账（按订单核对已收、退款与已生效结算合计并留痕，不改变任何单据状态）；以及工单的受理、读取与处理/解决/关闭状态流转（异常处理诉求登记成可跟踪的独立单据，不符对账未结清前禁止解决）。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /refunds`：受理退款单。请求字段 `refund_id`、`order_id`、`amount_cents`、`reason`；租户经 `X-Tenant` 传入，幂等键经 `Idempotency-Key` 请求头传入（必填）。受理成功返回 201，退款单为 `pending`。参数不合法（金额非正整数/超过订单金额）返回 400/422；订单不存在或跨租户返回 404（不泄漏对象是否存在）；退款标识重复受理返回 409；超过可退金额返回 409。
- `GET /refunds/{refund_id}`：按标识读取退款单，返回 `refund_id`、`order_id`、`amount_cents`、`reason`、`status`、`created_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /refunds/{refund_id}/reverse`：冲正退款单。对待处理（`pending`）或已生效（`effective`）退款单冲正后进入终态 `reversed` 并立即释放可退额度，返回 200；对已冲正退款单重复冲正返回 409 且不改变状态；不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /settlements`：受理结算单。请求字段 `settlement_id`、`order_id`、`amount_cents`（最小货币单位正整数）、`refund_ids`（非空、去重的退款标识集合）、`reason`；租户经 `X-Tenant` 传入，`Idempotency-Key` 必填。成功返回 201，结算单为 `pending`；参数不合法返回 400/422；订单不存在或跨租户返回 404（不泄漏对象是否存在）；结算标识重复受理返回 409。
- `GET /settlements/{settlement_id}`：按标识读取结算单，返回 `settlement_id`、`order_id`、`amount_cents`、`refund_ids`、`reason`、`status`、`created_at`、`updated_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /settlements/{settlement_id}/advance`：结算推进。原子地把引用的待处理退款全部置为 `effective` 且结算单进入 `effective`，返回 200；任一引用不满足则全部保持原状并返回可区分错误：引用退款不存在（含属于其他订单者）404，已被其他结算单核销 409，已冲正 409，结算金额与引用退款合计不符 409；结算单不存在或跨租户 404；已生效结算单重复推进 409；对已撤销结算单推进 409。需带 `Idempotency-Key`，同一（租户、操作、结算单标识、请求指纹）重放返回首次结果（含首次错误）；不同幂等键并发推进同一结算单仅一个成功，其余 409。
- `POST /settlements/{settlement_id}/revoke`：结算撤销。仅已生效（`effective`）结算单可撤销；原子地解除该结算单持有的全部核销声明、将其核销过且当前仍为 `effective` 的退款单置回 `pending`（继续占用可退额度，可被其他待处理结算单重新引用并在推进时核销），结算单进入终态 `revoked`，返回 200 与最新结算单对象。任一不满足则全部保持原状并返回可区分错误：结算单不存在或跨租户 404（不泄漏对象是否存在）；已撤销结算单重复撤销 409；待处理（`pending`）结算单撤销 409。撤销只解除核销关系，不改变退款单本身状态——推进后又被冲正的退款在撤销时保持 `reversed`。退款冲正仍只作用于退款单，与撤销互不替代。需带 `Idempotency-Key`，同一（租户、操作、结算单标识、请求指纹）重放返回首次结果（含首次错误，如首次为 404/409 则重放仍为同一 404/409），不产生第二次撤销或第二次状态翻转；不同幂等键并发撤销同一结算单仅一个生效，其余 409；撤销与推进并发作用于同一结算单或同一批退款时也仅一个操作生效，另一个被拒绝且不改变已存在数据。
- `POST /reconciliations`：发起并执行订单对账批次。请求字段 `batch_id`、`order_id`、`note`（核对说明，可空）；租户经 `X-Tenant` 传入，`Idempotency-Key` 必填。在同一事务内读取订单当前已收金额、待处理退款合计、已生效退款合计与已生效结算单合计，得出已核销结余（= 已收金额 −（待处理 + 已生效）退款合计）与核对结论（`balanced`/`mismatched`），批次进入 `completed` 并返回 201 与完整批次对象；对账只核对与留痕，不改变订单、退款、结算单的任何状态与金额。订单不存在或跨租户返回 404（不泄漏对象是否存在）；批次标识重复返回 409；同一订单已有进行中批次或执行期间有并发写入（收款、退款受理/冲正、结算推进/撤销、另一批次对账）返回可区分的 409，且整体失败、不留进行中批次或部分结论。同一订单同时只允许一个进行中的对账批次。
- `GET /reconciliations/{batch_id}`：按批次标识读取对账批次，返回 `batch_id`、`order_id`、`note`、`paid_cents`、`pending_refund_cents`、`effective_refund_cents`、`effective_settlement_cents`、`settled_balance_cents`、`conclusion`、`status`、`created_at`、`completed_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404（不泄漏对象是否存在）。已完成批次的结论不随订单后续收支变化而追溯修改，需要时以新批次标识重新发起即可。
- `POST /work-orders`：受理工单。请求字段 `work_order_id`、`order_id`、`issue`（问题说明，可空）；租户经 `X-Tenant` 传入，`Idempotency-Key` 必填。受理成功返回 201，工单为 `pending`（待处理），返回工单标识、订单标识、问题说明、解决说明（初始为空串）、状态、创建时间与更新时间。目标订单不存在或跨租户返回 404（不泄漏对象是否存在）；同一租户工单标识重复受理返回 409 且不改变已存在工单；参数不合法返回 400/422。
- `GET /work-orders/{work_order_id}`：按工单标识读取工单，返回 `work_order_id`、`order_id`、`issue`、`resolution`、`status`、`created_at`、`updated_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /work-orders/{work_order_id}/process`：工单处理。把待处理（`pending`）或处理中（`in_progress`）的工单推进/保持为 `in_progress`，返回 200 与最新工单对象；已解决（`resolved`）或已关闭（`closed`）的工单处理返回 409 且不改变状态。不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /work-orders/{work_order_id}/resolve`：工单解决。请求字段可选 `resolution`（解决说明，默认空串）；把待处理或处理中的工单置为 `resolved` 并记录解决说明，返回 200；已关闭工单解决返回 409 且不改变状态。若该工单所属订单存在结论为不符（`mismatched`）且已完成的对账批次、且其后没有更新的相符（`balanced`）已完成批次，则解决被拒绝并返回 409 且不改变工单状态；以新批次重新对账得到相符结论后才可解决。已关闭工单不受对账限制（但已关闭工单本身不可再解决）。不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /work-orders/{work_order_id}/close`：工单关闭。把待处理、处理中或已解决的工单置为终态 `closed`，返回 200；对已关闭工单重复关闭返回 409 且不改变状态。不受对账未结清限制。不存在或跨租户返回 404。需带 `Idempotency-Key`。

### 退款与幂等说明

- 可退金额 = 订单已收金额 − 待处理/已生效退款合计；`reversed` 退款不占用额度。
- 冲正为单事务原子操作，失败不会留下部分占用或部分释放。
- 结算推进为单事务原子操作：引用退款全部进入 `effective` 且结算单进入 `effective`，或全部保持原状；同一退款单只能被一张已生效结算单核销（声明表唯一约束兜底）。
- 结算撤销为单事务原子操作：结算单进入终态 `revoked`、其全部核销声明解除、退款回到 `pending`，要么一并发生要么全部保持原状。撤销后退款可被另一张结算单重新核销，但同一退款同一时刻仍至多被一张已生效结算单核销；撤销只解除核销关系，不改变退款单状态（与退款冲正互不替代）。
- 幂等重放以（租户, 操作, 目标标识, `Idempotency-Key`）去重：同一键重复提交返回与首次完全相同的业务结果（含首次错误），不会产生第二次受理、第二次扣减、第二次释放、第二次状态翻转或第二次撤销；重启后依然识别为重放。不同幂等键指向同一目标标识并发提交时仅一个生效，其余返回 409。
- 工单的登记、处理、解决与关闭同样按（租户, 操作, 工单标识, `Idempotency-Key`）幂等去重（操作分别为 `work_order_accept`/`work_order_process`/`work_order_resolve`/`work_order_close`）：同一指纹重放返回首次结果（含首次 404/409），不产生第二次登记或第二次状态翻转；不同指纹并发翻转同一工单仅一个生效，其余 409 且不改变已存在数据。工单的登记与状态翻转均为单事务原子操作，不改变订单、退款、结算单与对账批次的任何状态、金额与结论。

### 对账说明

- 对账批次为只读核对：发起即在同一事务内完成核对并留痕，批次状态由 `in_progress` 进入 `completed`（终态）；不改变订单、退款、结算单的任何状态与金额。
- 核对口径：已收金额 = 待处理退款合计 + 已生效退款合计 + 已核销结余，其中已核销结余 = 已收金额 −（待处理 + 已生效）退款合计；占用额度 ≤ 已收金额 ≤ 订单金额时结论为 `balanced`，否则为 `mismatched`。
- 同一订单同时只允许一个进行中的对账批次（部分唯一索引兜底）；并发发起时仅一个进入进行中，其余返回 409。
- 执行期间订单被收款、退款受理/冲正、结算推进/撤销等并发写入改动时，对账要么基于改动前的读数原子完成并留痕，要么整体失败并返回可区分的 409 冲突原因，不留进行中批次或部分结论。
- 对账幂等以（租户, 操作, 批次标识, `Idempotency-Key`）去重：重复提交返回与首次完全相同的业务结果（含首次错误），不产生第二次对账、第二次留痕；重启后仍识别为重放。
- 已完成批次的结论不随订单后续收支变化而追溯修改；需要重新核对时以新批次标识再次发起。

### 调用示例

```bash
# 受理退款（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/refunds -H 'X-Tenant: t1' -H 'Idempotency-Key: rfd-0001' \
  -H 'Content-Type: application/json' \
  -d '{"refund_id":"r1","order_id":"o1","amount_cents":300,"reason":"客户申请"}'

# 读取退款单
curl -s localhost:8000/refunds/r1 -H 'X-Tenant: t1'

# 冲正退款单（释放可退额度）
curl -s -X POST localhost:8000/refunds/r1/reverse \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: rev-0001'

# 受理结算单（引用退款集合，金额为最小货币单位整数且须等于推进时的退款合计）
curl -s -X POST localhost:8000/settlements -H 'X-Tenant: t1' -H 'Idempotency-Key: stl-0001' \
  -H 'Content-Type: application/json' \
  -d '{"settlement_id":"s1","order_id":"o1","amount_cents":300,"refund_ids":["r1"],"reason":"周期结算"}'

# 读取结算单
curl -s localhost:8000/settlements/s1 -H 'X-Tenant: t1'

# 结算推进：引用的待处理退款逐张生效，结算单进入 effective
curl -s -X POST localhost:8000/settlements/s1/advance \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: adv-0001'

# 结算撤销：解除全部核销，退款回到 pending，结算单进入 revoked（终态）
curl -s -X POST localhost:8000/settlements/s1/revoke \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: rev-stl-0001'

# 发起并执行订单对账（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/reconciliations -H 'X-Tenant: t1' -H 'Idempotency-Key: rec-0001' \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"b1","order_id":"o1","note":"月度对账"}'

# 按批次标识读取对账结果
curl -s localhost:8000/reconciliations/b1 -H 'X-Tenant: t1'

# 受理工单（异常处理诉求登记，同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/work-orders -H 'X-Tenant: t1' -H 'Idempotency-Key: wo-0001' \
  -H 'Content-Type: application/json' \
  -d '{"work_order_id":"w1","order_id":"o1","issue":"客户反馈少发货"}'

# 读取工单
curl -s localhost:8000/work-orders/w1 -H 'X-Tenant: t1'

# 处理：pending/in_progress -> in_progress
curl -s -X POST localhost:8000/work-orders/w1/process \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: wo-proc-0001'

# 解决：pending/in_progress -> resolved（存在未结清的不符对账批次时返回 409）
curl -s -X POST localhost:8000/work-orders/w1/resolve \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: wo-res-0001' \
  -H 'Content-Type: application/json' -d '{"resolution":"核实后已补发"}'

# 关闭：pending/in_progress/resolved -> closed（终态，不受对账限制）
curl -s -X POST localhost:8000/work-orders/w1/close \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: wo-close-0001'
```
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期。
- 退款受理后为待处理（`pending`），由结算推进（`POST /settlements` + `/advance`）原子推进为已生效（`effective`）；服务自身不做自动结算。
