import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, record_and_finish, store_outcome
from app.store.outcome import Outcome

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
WRITER_BUSY = "concurrent_write"

PAYMENT_OP = "order_payment"


def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}

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


def _order_body(row: sqlite3.Row) -> dict:
    return {**dict(row), "outstanding_cents": row["amount_cents"] - row["paid_cents"]}


def register_payment(tenant: str, order_id: str, amount_cents: int,
                     fingerprint: str) -> Outcome:
    """Register a payment on an order idempotently and atomically.

    Dedup identity is (tenant, operation, order_id, fingerprint): a replayed
    request returns the recorded first result — including a recorded 404/409 —
    without a second registration, a second paid_cents increment or a second
    status flip, and the record persists across restarts. Payment registration
    is not document-id dedup: the same order may take several payments under
    distinct fingerprints, each amount applied independently.

    The connection uses a zero lock timeout: if a concurrent write holds the
    write lock, the payment fails as a whole with a distinguishable 409 instead
    of blocking, so concurrent registrations under different fingerprints have
    exactly one winner and the losers change nothing (that 409 is not recorded,
    the caller may retry). A missing or cross-tenant order is indistinguishable
    (404, no existence leak); exceeding the outstanding amount conflicts (409)
    and is recorded for replay. Conservation holds: paid never exceeds the
    order amount and outstanding = amount - paid.
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

        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            return record_and_finish(
                conn, tenant=tenant, operation=PAYMENT_OP, target_id=order_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="payment exceeds outstanding amount",
            )

        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        updated = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status "
            "FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        body = _order_body(updated)
        store_outcome(conn, tenant=tenant, operation=PAYMENT_OP, target_id=order_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()
