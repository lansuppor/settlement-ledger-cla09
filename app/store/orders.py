import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, record_and_finish, store_outcome
from app.store.outcome import Outcome

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
QUOTA = "quota_exceeded"
WRITER_BUSY = "concurrent_write"

# Payment registration is request dedup only: it creates no business document,
# so the dedup target is the order itself.
PAYMENT_OP = "payment_accept"


def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()


def _order_body(conn: sqlite3.Connection, tenant: str, order_id: str) -> dict | None:
    row = conn.execute(
        "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    if row is None:
        return None
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}


def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        return _order_body(conn, tenant, order_id)
    finally:
        conn.close()


def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)


def register_payment(tenant: str, order_id: str, amount_cents: int,
                     fingerprint: str) -> Outcome:
    """Register a payment on an order idempotently and atomically.

    Dedup key is (tenant, operation, order_id, fingerprint): a replayed request
    returns the recorded first result (first error included) without a second
    paid_cents increment or a second status flip; the dedup record is persisted,
    so a replay is still recognised after a restart. The payment itself creates
    no business document — the idempotency key is request dedup only, and the
    same key against a different order replays against that order's own first
    result. Distinct fingerprints may register several payments on one order,
    each amount applied independently.

    The connection uses a zero lock timeout: if a concurrent write (another
    payment, refund, settlement, reconciliation or import) holds the write
    lock, this registration fails with a distinguishable 409 instead of
    blocking, and nothing is changed or recorded.
    """
    conn = connect(timeout=0)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "lock" in str(error).lower():
                return Outcome(WRITER_BUSY, 409,
                               "a concurrent write is in progress; retry the payment", {})
            raise

        replay = lookup_replay(conn, tenant, PAYMENT_OP, order_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation=PAYMENT_OP, target_id=order_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="order not found",
            )

        # Conservation: receivable = paid + outstanding; paid never exceeds the
        # order amount. An over-payment changes nothing.
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            return record_and_finish(
                conn, tenant=tenant, operation=PAYMENT_OP, target_id=order_id,
                fingerprint=fingerprint, status=409, code=QUOTA,
                detail="payment exceeds outstanding amount",
            )

        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        body = _order_body(conn, tenant, order_id)
        store_outcome(conn, tenant=tenant, operation=PAYMENT_OP, target_id=order_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()
