# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；退款单的受理、读取、冲正与幂等重放；以及结算单的受理、读取与结算推进（把引用的待处理退款原子地推进为已生效）。数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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

### 结算单

- `POST /settlements`：受理结算单。请求字段 `settlement_id`、`order_id`、`amount_cents`（正整数，最小货币单位）、`refund_ids`（非空字符串数组）、`reason`；租户经 `X-Tenant` 传入，幂等键经 `Idempotency-Key` 请求头传入（均必填）。成功返回 201，结算单为 `pending`，并原样返回引用的退款标识集合。受理为薄校验：仅校验订单存在且同租户（不存在或跨租户一律 404，不泄漏对象是否存在）、结算标识未重复受理（重复 409）、退款集合非空且不重复（400）；引用的退款单是否存在、是否已核销/冲正、金额合计是否相符等留待推进时判定。
- `GET /settlements/{settlement_id}`：按标识读取结算单，返回 `settlement_id`、`order_id`、`amount_cents`、`refund_ids`、`reason`、`status`、`created_at`、`updated_at`；租户经 `X-Tenant` 隔离，不存在或跨租户返回 404。
- `POST /settlements/{settlement_id}/advance`：结算推进。把引用的待处理退款逐张置为 `effective`（真正占用可退额度），同时把结算单置为 `effective`，返回 200 与最新结算单对象。推进必须原子：任一项不满足即整体不改并返回可区分的失败——引用退款单不存在/跨租户（404 `not_found`）、引用退款属于其他订单（409 `refund_order_mismatch`）、已冲正（409 `refund_reversed`）、已被其他结算单核销（409 `refund_already_settled`）、结算金额与引用退款合计不符（400 `amount_mismatch`）；已生效结算单重复推进或并发落败返回 409（`settlement_already_effective`/`refund_already_settled`）。需带 `Idempotency-Key`。

### 退款与幂等说明

- 可退金额 = 订单已收金额 −（待处理 + 已生效）退款合计；`reversed` 退款不占用额度。
- 冲正为单事务原子操作，失败不会留下部分占用或部分释放。
- 幂等重放以（租户, 操作, 目标标识, `Idempotency-Key`）去重：同一键重复提交返回与首次完全相同的业务结果（含首次错误），不会产生第二次受理、第二次扣减或第二次释放；重启后依然识别为重放。不同幂等键指向同一退款标识并发提交时仅一个生效，其余返回 409。

### 结算与幂等说明

- 结算单是独立单据；结算单受理本身不改变退款状态、不额外占用额度，占用额度的是退款单（待处理与已生效均占用），推进只是把待处理退款确认为已生效。
- 同一张退款单只能被一个结算单核销一次：待处理退款允许同时被多张待处理结算单引用，`BEGIN IMMEDIATE` 把并发推进串行化，先成功者把退款置为已生效，其余推进引用冲突失败且不改变数据。
- 推进原子性由单事务保证：校验与状态翻转在同一写事务内，失败即回滚，不留下部分核销或第二次额度占用。
- 幂等去重以（租户, 操作, 目标标识, `Idempotency-Key`）为准，复用退款的 `idempotent_requests` 表（结算操作为 `settle_accept`/`settle_advance`）：同一指纹重复提交返回与首次完全相同的业务结果（含首次错误），不产生第二次状态翻转或额度占用，且重启后仍识别为重放。不同幂等键指向同一结算单并发推进只允许一个生效。
- 结算推进的错误响应体为 `detail: {"code": ..., "message": ...}`，便于程序化区分参数不合法（400/422）、对象不存在（404）、重复受理（409）、重复推进/引用冲突（409）、金额不符（400）与内部错误（500）。

### 调用示例

```bash
# 受理两张待处理退款
curl -s -X POST localhost:8000/refunds -H 'X-Tenant: t1' -H 'Idempotency-Key: rfd-0001' \
  -H 'Content-Type: application/json' \
  -d '{"refund_id":"r1","order_id":"o1","amount_cents":300,"reason":"客户申请"}'
curl -s -X POST localhost:8000/refunds -H 'X-Tenant: t1' -H 'Idempotency-Key: rfd-0002' \
  -H 'Content-Type: application/json' \
  -d '{"refund_id":"r2","order_id":"o1","amount_cents":200,"reason":"客户申请"}'
curl -s localhost:8000/refunds/r1 -H 'X-Tenant: t1'

# 受理结算单（同一 Idempotency-Key 可安全重放）
curl -s -X POST localhost:8000/settlements -H 'X-Tenant: t1' -H 'Idempotency-Key: stl-0001' \
  -H 'Content-Type: application/json' \
  -d '{"settlement_id":"s1","order_id":"o1","amount_cents":500,"refund_ids":["r1","r2"],"reason":"日结"}'

# 读取结算单
curl -s localhost:8000/settlements/s1 -H 'X-Tenant: t1'

# 结算推进：引用的待处理退款全部变为 effective，结算单变为 effective
curl -s -X POST localhost:8000/settlements/s1/advance \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: adv-0001'

# 冲正退款单（释放可退额度）
curl -s -X POST localhost:8000/refunds/r1/reverse \
  -H 'X-Tenant: t1' -H 'Idempotency-Key: rev-0001'
```

`GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账。
- 退款受理后为待处理（`pending`），由结算单推进（`POST /settlements/{id}/advance`）确认为已生效（`effective`）；已被已生效结算单核销的退款不得再被其他结算单核销（同一张退款只核销一次）。
