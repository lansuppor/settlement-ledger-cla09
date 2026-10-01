import argparse
import hashlib
import json

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, refunds
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")


class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)


class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)


class RefundIn(BaseModel):
    refund_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=1)


@app.exception_handler(RequestValidationError)
def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # 参数不合法统一 400（与既有接口约定一致）。
    return JSONResponse(status_code=400, content={"detail": "invalid parameters", "errors": exc.errors()})


def _require_tenant(x_tenant: str) -> str:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return x_tenant


def _fingerprint(idempotency_key: str, payload: dict) -> str:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((idempotency_key + "|" + canonical).encode("utf-8")).hexdigest()


@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/orders", status_code=201)
def create_order(body: OrderIn) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)


@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


@app.post("/refunds", status_code=201)
def accept_refund(body: RefundIn, x_tenant: str = Header(default=""),
                  idempotency_key: str = Header(default="", alias="Idempotency-Key")) -> dict:
    tenant = _require_tenant(x_tenant)
    fingerprint = _fingerprint(
        idempotency_key,
        {"refund_id": body.refund_id, "order_id": body.order_id,
         "amount_cents": body.amount_cents, "reason": body.reason},
    )
    try:
        result = refunds.accept(tenant, body.refund_id, body.order_id,
                                body.amount_cents, body.reason, fingerprint)
    except refunds.ApiError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    if isinstance(result, refunds.Replayed):
        if result.status_code != 201:
            raise HTTPException(status_code=result.status_code, detail=result.body["detail"])
        # 首次成功的重放仍返回首次的退款对象（201）。
        return result.body
    return result


@app.get("/refunds/{refund_id}")
def read_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    tenant = _require_tenant(x_tenant)
    refund = refunds.get(tenant, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund


@app.post("/refunds/{refund_id}/reversals", status_code=200)
def reverse_refund(refund_id: str, x_tenant: str = Header(default=""),
                   idempotency_key: str = Header(default="", alias="Idempotency-Key")) -> dict:
    tenant = _require_tenant(x_tenant)
    fingerprint = _fingerprint(idempotency_key, {"refund_id": refund_id})
    try:
        result = refunds.reverse(tenant, refund_id, fingerprint)
    except refunds.ApiError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail)
    if isinstance(result, refunds.Replayed):
        if result.status_code != 200:
            raise HTTPException(status_code=result.status_code, detail=result.body["detail"])
        return result.body
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
