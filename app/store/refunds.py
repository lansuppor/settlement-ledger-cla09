import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome

# Refund lifecycle: pending -> effective -> reversed (terminal).
# pending and effective refunds occupy the order's refundable quota;
# reversed refunds release it.
PENDING = "pending"
EFFECTIVE = "effective"
REVERSED = "reversed"

# Outcome codes let the HTTP layer distinguish error classes without
# parsing free-text messages.
NOT_FOUND = "not_found"
QUOTA = "quota_exceeded"
CONFLICT = "conflict"


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "refund_id": row["refund_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "reason": row["reason"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def get(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, refund_id, order_id, amount_cents, reason, status, created_at "
            "FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_dict(row)


def accept(tenant: str, refund_id: str, order_id: str, amount_cents: int,
           reason: str, fingerprint: str) -> Outcome:
    """Accept a refund idempotently.

    Refundable quota = order.paid_cents - SUM(amount) of pending/effective
    refunds. A replayed request (same tenant/operation/target/fingerprint)
    returns the original result regardless of current quota.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, "accept", refund_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return record_and_finish(
                conn, tenant=tenant, operation="accept", target_id=refund_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="refund target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation="accept", target_id=refund_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="refund already accepted",
            )

        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM refunds "
            "WHERE tenant=? AND order_id=? AND status IN (?,?)",
            (tenant, order_id, PENDING, EFFECTIVE),
        ).fetchone()["s"]
        refundable = order["paid_cents"] - occupied

        # A positive amount that also fits the refundable quota necessarily
        # fits the order amount; the order-amount cap is checked explicitly
        # to make the boundary explicit.
        if amount_cents <= 0 or amount_cents > order["amount_cents"]:
            return record_and_finish(
                conn, tenant=tenant, operation="accept", target_id=refund_id,
                fingerprint=fingerprint, status=400, code="invalid_amount",
                detail="refund amount must be a positive integer not exceeding order amount",
            )
        if amount_cents > refundable:
            return record_and_finish(
                conn, tenant=tenant, operation="accept", target_id=refund_id,
                fingerprint=fingerprint, status=409, code=QUOTA,
                detail="refund exceeds refundable amount",
            )

        created_at = now_iso()
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, reason, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, refund_id, order_id, amount_cents, reason, PENDING, created_at),
        )
        body = {
            "refund_id": refund_id,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "reason": reason,
            "status": PENDING,
            "created_at": created_at,
        }
        store_outcome(conn, tenant=tenant, operation="accept", target_id=refund_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def reverse(tenant: str, refund_id: str, fingerprint: str) -> Outcome:
    """Reverse a refund idempotently, releasing its occupied quota atomically."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, "reverse", refund_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            "SELECT order_id, amount_cents, reason, status, created_at FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation="reverse", target_id=refund_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="refund not found",
            )

        if row["status"] == REVERSED:
            return record_and_finish(
                conn, tenant=tenant, operation="reverse", target_id=refund_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="refund already reversed",
            )

        conn.execute(
            "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=?",
            (REVERSED, tenant, refund_id),
        )
        body = {
            "refund_id": refund_id,
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "reason": row["reason"],
            "status": REVERSED,
            "created_at": row["created_at"],
        }
        store_outcome(conn, tenant=tenant, operation="reverse", target_id=refund_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()
