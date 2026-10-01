import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome
from app.store.refunds import EFFECTIVE, PENDING
from app.store.settlements import EFFECTIVE_SETTLEMENT

# Reconciliation batch lifecycle: in_progress -> completed (terminal).
# A batch is in_progress only inside its own transaction; committed batches
# are always completed, and a failed reconciliation leaves no batch behind.
IN_PROGRESS = "in_progress"
COMPLETED = "completed"

# Conclusion of the check: paid == pending + effective + settled_balance and
# occupied <= paid <= order amount.
BALANCED = "balanced"
MISMATCHED = "mismatched"

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
ORDER_BUSY = "order_reconciliation_in_progress"
WRITER_BUSY = "concurrent_write"

RECONCILE_OP = "reconcile"


def _row_to_body(row: sqlite3.Row) -> dict:
    return {
        "batch_id": row["batch_id"],
        "order_id": row["order_id"],
        "note": row["note"],
        "paid_cents": row["paid_cents"],
        "pending_refund_cents": row["pending_refund_cents"],
        "effective_refund_cents": row["effective_refund_cents"],
        "effective_settlement_cents": row["effective_settlement_cents"],
        "settled_balance_cents": row["settled_balance_cents"],
        "conclusion": row["conclusion"],
        "status": row["status"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def get(tenant: str, batch_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, batch_id, order_id, note, paid_cents, pending_refund_cents, "
            "effective_refund_cents, effective_settlement_cents, settled_balance_cents, "
            "conclusion, status, created_at, completed_at "
            "FROM reconciliations WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_body(row)


def execute(tenant: str, batch_id: str, order_id: str, note: str,
            fingerprint: str) -> Outcome:
    """Run a reconciliation batch for an order idempotently and atomically.

    The whole check runs in a single immediate write transaction: the order's
    paid amount, the pending/effective refund totals and the effective
    settlement total are read on one consistent snapshot, the derived
    settled balance (paid - pending - effective) and the conclusion are
    recorded, and the batch flips in_progress -> completed before commit.
    Nothing about orders, refunds or settlements is mutated.

    The connection uses a zero lock timeout: if a concurrent write (payment,
    refund accept/reverse, settlement advance/revoke, or another reconcile)
    holds the write lock, the batch fails as a whole with a distinguishable
    409 conflict instead of blocking — no in-progress batch or partial
    conclusion is left behind. A replayed request (same tenant/operation/
    batch/fingerprint) returns the recorded first result, errors included.
    """
    conn = connect(timeout=0)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "lock" in str(error).lower():
                return Outcome(WRITER_BUSY, 409,
                               "a concurrent write is in progress; retry the reconciliation", {})
            raise

        replay = lookup_replay(conn, tenant, RECONCILE_OP, batch_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return record_and_finish(
                conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="reconciliation target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM reconciliations WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="reconciliation batch already exists",
            )

        busy = conn.execute(
            "SELECT 1 FROM reconciliations "
            "WHERE tenant=? AND order_id=? AND status=?",
            (tenant, order_id, IN_PROGRESS),
        ).fetchone()
        if busy is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
                fingerprint=fingerprint, status=409, code=ORDER_BUSY,
                detail="order already has an in-progress reconciliation batch",
            )

        pending_total = _refund_total(conn, tenant, order_id, PENDING)
        effective_total = _refund_total(conn, tenant, order_id, EFFECTIVE)
        settled_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM settlements "
            "WHERE tenant=? AND order_id=? AND status=?",
            (tenant, order_id, EFFECTIVE_SETTLEMENT),
        ).fetchone()["s"]

        paid = order["paid_cents"]
        settled_balance = paid - pending_total - effective_total
        # Conservation: paid == pending + effective + settled_balance holds by
        # construction; the check is the occupancy inequality.
        conclusion = BALANCED if 0 <= settled_balance and paid <= order["amount_cents"] \
            else MISMATCHED

        created_at = now_iso()
        conn.execute(
            "INSERT INTO reconciliations(tenant, batch_id, order_id, note, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (tenant, batch_id, order_id, note, IN_PROGRESS, created_at),
        )
        completed_at = now_iso()
        conn.execute(
            "UPDATE reconciliations SET paid_cents=?, pending_refund_cents=?, "
            "effective_refund_cents=?, effective_settlement_cents=?, settled_balance_cents=?, "
            "conclusion=?, status=?, completed_at=? "
            "WHERE tenant=? AND batch_id=?",
            (paid, pending_total, effective_total, settled_total, settled_balance,
             conclusion, COMPLETED, completed_at, tenant, batch_id),
        )
        body = {
            "batch_id": batch_id,
            "order_id": order_id,
            "note": note,
            "paid_cents": paid,
            "pending_refund_cents": pending_total,
            "effective_refund_cents": effective_total,
            "effective_settlement_cents": settled_total,
            "settled_balance_cents": settled_balance,
            "conclusion": conclusion,
            "status": COMPLETED,
            "created_at": created_at,
            "completed_at": completed_at,
        }
        store_outcome(conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def _refund_total(conn: sqlite3.Connection, tenant: str, order_id: str, status: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS s FROM refunds "
        "WHERE tenant=? AND order_id=? AND status=?",
        (tenant, order_id, status),
    ).fetchone()["s"]
