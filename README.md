# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；退款单的受理、读取、冲正与幂等重放；结算单的受理、读取、推进（把引用的待处理退款原子推进为已生效）与撤销（解除已生效结算单的全部核销，退款回到待处理）；订单对账（按订单核对已收、退款与已生效结算合计并留痕，不改变任何单据状态）；以及异常工单的登记、读取、处理、解决与关闭（把异常处理诉求登记成可跟踪的独立单据，解决受对账未结清冲突约束）。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /tickets`：登记异常工单。请求字段 `ticket_id`、`order_id`、`issue`（问题说明，非空）；租户经 `X-Tenant` 传入，`Idempotency-Key` 必填。成功返回 201，工单为 `pending`（待处理）。参数不合法返回 400/422；目标订单不存在或跨租户返回 404（一律按不存在处理，不泄漏对象是否存在，且不留工单）；同一租户工单标识重复受理返回 409 且不改变已存在工单。
- `GET /tickets/{ticket_id}`：按标识读取工单，返回 `ticket_id`、`order_id`、`issue`、`status`、`created_at`、`updated_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /tickets/{ticket_id}/process`：处理工单。把待处理（`pending`）工单推进为处理中（`processing`）；对已处于 `processing` 的工单处理为幂等成功（不再次翻转、不改变更新时间）；已解决（`resolved`）或已关闭（`closed`）工单处理返回 409 且不改变状态；不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /tickets/{ticket_id}/resolve`：解决工单。把 `pending` 或 `processing` 工单置为 `resolved`，并记录请求字段 `resolution_note`（解决说明，非空）。已关闭工单解决返回 409；对已解决工单再次解决返回 409。**对账未结清冲突**：若该工单所属订单存在结论为不符（`mismatched`）且已完成（`completed`）的对账批次（以最新一条已完成批次的结论为准），解决被拒绝返回 409 且不改变工单状态；需先以新批次重新对账得到相符（`balanced`）结论后才可解决。已关闭工单不受此限制。不存在或跨租户返回 404。需带 `Idempotency-Key`。
- `POST /tickets/{ticket_id}/close`：关闭工单。把 `pending`、`processing` 或 `resolved` 工单置为终态 `closed`，返回 200；对已关闭工单重复关闭返回 409 且不改变状态；不存在或跨租户返回 404。需带 `Idempotency-Key`。

### 工单与对账未结清说明

- 工单状态机：待处理（`pending`）→ 处理中（`processing`）→ 已解决（`resolved`）→ 已关闭（`closed`，终态）；`pending`/`processing` 也可直接解决或关闭。受理成功即为 `pending`。
- 所有状态翻转均为单事务原子操作，失败不改变任何已存在数据；工单的登记与翻转只作用于工单本身，不改变订单、退款、结算单与对账批次的任何状态、金额与结论。
- 解决闸门以「该订单最新一条已完成对账批次的结论」为准：最新为 `mismatched` 即拒绝解决（409，可区分错误）；最新为 `balanced`（或尚无已完成批次）才允许解决。历史不符批次不追溯、不阻塞——以新批次重新对账得到相符即可。
- 工单四类写操作（登记、处理、解决、关闭）均以（租户, 操作, 工单标识, `Idempotency-Key`）幂等去重：同一指纹重复提交返回与首次完全相同的业务结果（含首次错误，如首次为 404/409 则重放仍为同一 404/409），不产生第二次登记或第二次状态翻转；去重记录持久化落库，重启后仍识别为重放。不同指纹指向同一工单并发提交时仅一个生效，其余 409 且不改变已存在数据。
- 错误可区分：参数不合法 400/422；对象不存在 404；重复受理 409；非法状态翻转 409；对账未结清冲突 409（detail 以 `order has a mismatched reconciliation batch` 开头）。

### 退款与幂等说明

- 可退金额 = 订单已收金额 − 待处理/已生效退款合计；`reversed` 退款不占用额度。
- 冲正为单事务原子操作，失败不会留下部分占用或部分释放。
- 结算推进为单事务原子操作：引用退款全部进入 `effective` 且结算单进入 `effective`，或全部保持原状；同一退款单只能被一张已生效结算单核销（声明表唯一约束兜底）。
- 结算撤销为单事务原子操作：结算单进入终态 `revoked`、其全部核销声明解除、退款回到 `pending`，要么一并发生要么全部保持原状。撤销后退款可被另一张结算单重新核销，但同一退款同一时刻仍至多被一张已生效结算单核销；撤销只解除核销关系，不改变退款单状态（与退款冲正互不替代）。
- 幂等重放以（租户, 操作, 目标标识, `Idempotency-Key`）去重：同一键重复提交返回与首次完全相同的业务结果（含首次错误），不会产生第二次受理、第二次扣减、第二次释放、第二次状态翻转或第二次撤销；重启后依然识别为重放。不同幂等键指向同一目标标识并发提交时仅一个生效，其余返回 409。工单的登记、处理、解决、关闭同样按此口径去重。

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

# 登记异常工单（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/tickets -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-0001' \
  -H 'Content-Type: application/json' \
  -d '{"ticket_id":"w1","order_id":"o1","issue":"收到商品与描述不符"}'

# 读取工单
curl -s localhost:8000/tickets/w1 -H 'X-Tenant: t1'

# 处理：待处理 -> 处理中（对处理中工单重复处理为幂等成功）
curl -s -X POST localhost:8000/tickets/w1/process \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-proc-0001'

# 解决：待处理/处理中 -> 已解决，并记录解决说明
# （若订单最新已完成对账批次结论为 mismatched，会返回 409 对账未结清冲突）
curl -s -X POST localhost:8000/tickets/w1/resolve \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-res-0001' \
  -H 'Content-Type: application/json' -d '{"resolution_note":"已补发并致歉"}'

# 关闭：任意非终态 -> 已关闭（终态）
curl -s -X POST localhost:8000/tickets/w1/close \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: tkt-close-0001'
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
