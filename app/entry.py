import argparse
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from app.config import tenant_header
from app.store import orders, refunds
from app.store.db import connect, migrate
from app.rules import order_rules

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
    reason: str = Field(default="")

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
def create_refund(body: RefundIn, x_tenant: str = Header(default=""),
                  idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = refunds.accept(x_tenant, body.refund_id, body.order_id,
                             body.amount_cents, body.reason, idempotency_key)
    return _render(outcome)

@app.get("/refunds/{refund_id}")
def read_refund(refund_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    refund = refunds.get(x_tenant, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="refund not found")
    return refund

@app.post("/refunds/{refund_id}/reverse", status_code=200)
def reverse_refund(refund_id: str, x_tenant: str = Header(default=""),
                   idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = refunds.reverse(x_tenant, refund_id, idempotency_key)
    return _render(outcome)

def _render(outcome: refunds.Outcome) -> dict:
    if outcome.code != "ok":
        raise HTTPException(status_code=outcome.status, detail=outcome.detail)
    return outcome.body

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
