# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /refunds`：受理退款。请求字段 `refund_id`、`order_id`、`amount_cents`、`reason`；租户通过请求头 `X-Tenant` 传入；须携带请求头 `Idempotency-Key` 作为请求指纹。成功返回 201 与退款单（`status=pending`）。参数不合法（含金额非正整数、超过订单金额、缺少租户/幂等头）返回 400；重复受理返回 409；超过可退金额（已收 − 已生效退款合计）返回 409；订单不存在或跨租户返回 404（不泄漏对象是否存在）。
- `GET /refunds/{refund_id}`：按退款标识读取，返回退款标识、订单标识、金额、原因、状态与创建时间；按租户隔离，跨租户/不存在返回 404。
- `POST /refunds/{refund_id}/reversals`：冲正退款（须携带 `Idempotency-Key`）。对 `pending`/`effective` 退款单冲正后进入 `reversed` 终态并立即释放可退额度，返回 200；对已冲正单重复冲正返回 409 且不改变状态；不存在返回 404。
- `GET /health`：返回服务与数据库状态。

### 退款调用示例

```bash
# 受理订单并收款（既有能力）
curl -s -X POST localhost:8000/orders -d '{"tenant":"t1","order_id":"o1","amount_cents":1000,"currency":"CNY"}' -H 'Content-Type: application/json'
curl -s -X POST localhost:8000/orders/o1/payments -d '{"amount_cents":800}' -H 'X-Tenant: t1'

# 受理退款（同一 Idempotency-Key + 相同报文 = 幂等重放，返回与首次一致的结果）
curl -s -X POST localhost:8000/refunds -H 'X-Tenant: t1' -H 'Idempotency-Key: r1' -H 'Content-Type: application/json' \
  -d '{"refund_id":"rf1","order_id":"o1","amount_cents":500,"reason":"customer request"}'

# 读取退款单
curl -s localhost:8000/refunds/rf1 -H 'X-Tenant: t1'

# 冲正（释放额度；同一 Idempotency-Key 重放返回首次结果，不同 Key 重复冲正返回 409）
curl -s -X POST localhost:8000/refunds/rf1/reversals -H 'X-Tenant: t1' -H 'Idempotency-Key: rv1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款只支持整单登记，未实现分期与对账。
