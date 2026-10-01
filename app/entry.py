import argparse
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from app.config import tenant_header
from app.store import orders, reconciliations, refunds, settlements, work_orders
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

class SettlementIn(BaseModel):
    settlement_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    refund_ids: list[str] = Field(min_length=1)
    reason: str = Field(default="")

class ReconciliationIn(BaseModel):
    batch_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    note: str = Field(default="")

class WorkOrderIn(BaseModel):
    work_order_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    issue: str = Field(default="")

class WorkOrderResolveIn(BaseModel):
    resolution: str = Field(default="")

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

@app.post("/settlements", status_code=201)
def create_settlement(body: SettlementIn, x_tenant: str = Header(default=""),
                      idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = settlements.accept(x_tenant, body.settlement_id, body.order_id,
                                 body.amount_cents, body.reason, body.refund_ids,
                                 idempotency_key)
    return _render(outcome)

@app.get("/settlements/{settlement_id}")
def read_settlement(settlement_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    settlement = settlements.get(x_tenant, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="settlement not found")
    return settlement

@app.post("/settlements/{settlement_id}/advance", status_code=200)
def advance_settlement(settlement_id: str, x_tenant: str = Header(default=""),
                       idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = settlements.advance(x_tenant, settlement_id, idempotency_key)
    return _render(outcome)

@app.post("/settlements/{settlement_id}/revoke", status_code=200)
def revoke_settlement(settlement_id: str, x_tenant: str = Header(default=""),
                      idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = settlements.revoke(x_tenant, settlement_id, idempotency_key)
    return _render(outcome)

@app.post("/reconciliations", status_code=201)
def create_reconciliation(body: ReconciliationIn, x_tenant: str = Header(default=""),
                          idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = reconciliations.execute(x_tenant, body.batch_id, body.order_id,
                                      body.note, idempotency_key)
    return _render(outcome)

@app.get("/reconciliations/{batch_id}")
def read_reconciliation(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    batch = reconciliations.get(x_tenant, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="reconciliation batch not found")
    return batch

@app.post("/work-orders", status_code=201)
def create_work_order(body: WorkOrderIn, x_tenant: str = Header(default=""),
                      idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = work_orders.accept(x_tenant, body.work_order_id, body.order_id,
                                 body.issue, idempotency_key)
    return _render(outcome)

@app.get("/work-orders/{work_order_id}")
def read_work_order(work_order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    work_order = work_orders.get(x_tenant, work_order_id)
    if work_order is None:
        raise HTTPException(status_code=404, detail="work order not found")
    return work_order

@app.post("/work-orders/{work_order_id}/process", status_code=200)
def process_work_order(work_order_id: str, x_tenant: str = Header(default=""),
                       idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = work_orders.process(x_tenant, work_order_id, idempotency_key)
    return _render(outcome)

@app.post("/work-orders/{work_order_id}/resolve", status_code=200)
def resolve_work_order(work_order_id: str, body: WorkOrderResolveIn,
                       x_tenant: str = Header(default=""),
                       idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = work_orders.resolve(x_tenant, work_order_id, body.resolution,
                                  idempotency_key)
    return _render(outcome)

@app.post("/work-orders/{work_order_id}/close", status_code=200)
def close_work_order(work_order_id: str, x_tenant: str = Header(default=""),
                     idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = work_orders.close(x_tenant, work_order_id, idempotency_key)
    return _render(outcome)

def _render(outcome) -> dict:
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
