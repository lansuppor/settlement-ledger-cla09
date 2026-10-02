import argparse
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field, StrictInt

from app.config import tenant_header
from app.rules import order_rules
from app.store import order_imports, orders, reconciliations, refunds, settlements, stock_movements, tickets
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

class TicketIn(BaseModel):
    ticket_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    issue: str = Field(min_length=1)

class TicketResolutionIn(BaseModel):
    resolution_note: str = Field(min_length=1)

class OrderImportIn(BaseModel):
    task_id: str = Field(min_length=1)
    csv_content: str = Field(min_length=1)

class StockMovementIn(BaseModel):
    movement_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    direction: Literal["in", "out"]
    quantity: StrictInt = Field(gt=0)

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
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default=""),
                idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    return _render(orders.register_payment(x_tenant, order_id, body.amount_cents,
                                           idempotency_key))

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

@app.post("/tickets", status_code=201)
def create_ticket(body: TicketIn, x_tenant: str = Header(default=""),
                  idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = tickets.accept(x_tenant, body.ticket_id, body.order_id,
                             body.issue, idempotency_key)
    return _render(outcome)

@app.get("/tickets/{ticket_id}")
def read_ticket(ticket_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    ticket = tickets.get(x_tenant, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return ticket

@app.post("/tickets/{ticket_id}/process", status_code=200)
def process_ticket(ticket_id: str, x_tenant: str = Header(default=""),
                   idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = tickets.process(x_tenant, ticket_id, idempotency_key)
    return _render(outcome)

@app.post("/tickets/{ticket_id}/resolve", status_code=200)
def resolve_ticket(ticket_id: str, body: TicketResolutionIn,
                   x_tenant: str = Header(default=""),
                   idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = tickets.resolve(x_tenant, ticket_id, body.resolution_note, idempotency_key)
    return _render(outcome)

@app.post("/tickets/{ticket_id}/close", status_code=200)
def close_ticket(ticket_id: str, x_tenant: str = Header(default=""),
                 idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = tickets.close(x_tenant, ticket_id, idempotency_key)
    return _render(outcome)

def _render(outcome) -> dict:
    if outcome.code != "ok":
        raise HTTPException(status_code=outcome.status, detail=outcome.detail)
    return outcome.body

@app.post("/order-imports", status_code=201)
def create_order_import(body: OrderImportIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        outcome = order_imports.accept(x_tenant, body.task_id, body.csv_content)
    except order_imports.InvalidImportRequest as error:
        raise HTTPException(status_code=400, detail=str(error))
    return _render(outcome)

@app.get("/order-imports/{task_id}")
def read_order_import(task_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    task = order_imports.get(x_tenant, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="import task not found")
    return task

@app.post("/order-imports/{task_id}/resume", status_code=200)
def resume_order_import(task_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    return _render(order_imports.resume(x_tenant, task_id))

@app.post("/stock-movements", status_code=201)
def create_stock_movement(body: StockMovementIn, x_tenant: str = Header(default=""),
                          idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = stock_movements.accept(x_tenant, body.movement_id, body.order_id,
                                     body.direction, body.quantity, idempotency_key)
    return _render(outcome)

@app.get("/stock-movements/summary")
def summarize_stock_movements(
    x_tenant: str = Header(default=""),
    order_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=stock_movements.DEFAULT_PAGE_LIMIT, ge=1,
                       le=stock_movements.MAX_PAGE_LIMIT),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        return stock_movements.summary(x_tenant, order_id=order_id, cursor=cursor, limit=limit)
    except stock_movements.InvalidCursor:
        raise HTTPException(status_code=400, detail="cursor is invalid")
    except stock_movements.OrderSummaryNotFound:
        raise HTTPException(status_code=400,
                            detail="order has no stock movement summary")

@app.get("/stock-movements/monthly-summary")
def summarize_stock_movements_monthly(
    x_tenant: str = Header(default=""),
    order_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=stock_movements.DEFAULT_PAGE_LIMIT, ge=1,
                       le=stock_movements.MAX_PAGE_LIMIT),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        return stock_movements.monthly_summary(x_tenant, order_id=order_id,
                                               cursor=cursor, limit=limit)
    except stock_movements.InvalidCursor:
        raise HTTPException(status_code=400, detail="cursor is invalid")
    except stock_movements.OrderSummaryNotFound:
        raise HTTPException(status_code=400,
                            detail="order has no stock movement summary")

@app.get("/stock-movements/{movement_id}")
def read_stock_movement(movement_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    movement = stock_movements.get(x_tenant, movement_id)
    if movement is None:
        raise HTTPException(status_code=404, detail="stock movement not found")
    return movement

@app.get("/stock-movements/{movement_id}/events")
def read_stock_movement_events(movement_id: str, x_tenant: str = Header(default="")) -> list:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    trail = stock_movements.events(x_tenant, movement_id)
    if trail is None:
        raise HTTPException(status_code=404, detail="stock movement not found")
    return trail

@app.post("/stock-movements/{movement_id}/reverse", status_code=200)
def reverse_stock_movement(movement_id: str, x_tenant: str = Header(default=""),
                           idempotency_key: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency-key header is required")
    outcome = stock_movements.reverse(x_tenant, movement_id, idempotency_key)
    return _render(outcome)

@app.get("/stock-movements")
def list_stock_movements(
    x_tenant: str = Header(default=""),
    order_id: str | None = Query(default=None),
    direction: Literal["in", "out"] | None = Query(default=None),
    quantity_min: int | None = Query(default=None, ge=1),
    quantity_max: int | None = Query(default=None, ge=1),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
    status: Literal["accepted", "reversed"] | None = Query(default=None),
    include_reversed: bool | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=stock_movements.DEFAULT_PAGE_LIMIT, ge=1,
                       le=stock_movements.MAX_PAGE_LIMIT),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if quantity_min is not None and quantity_max is not None and quantity_min > quantity_max:
        raise HTTPException(status_code=400, detail="quantity_min must not be greater than quantity_max")
    if status is not None and include_reversed is not None:
        raise HTTPException(
            status_code=400,
            detail="status and include_reversed are mutually exclusive",
        )
    try:
        return stock_movements.search(
            x_tenant, order_id=order_id, direction=direction,
            quantity_min=quantity_min, quantity_max=quantity_max,
            created_from=created_from, created_to=created_to,
            status=status, include_reversed=bool(include_reversed),
            cursor=cursor, limit=limit,
        )
    except stock_movements.InvalidCursor:
        raise HTTPException(status_code=400, detail="cursor is invalid")

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
