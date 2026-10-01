import sqlite3
import threading
from collections import defaultdict

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome
from app.store.refunds import EFFECTIVE as EFFECTIVE_REFUND
from app.store.refunds import PENDING as PENDING_REFUND
from app.store.settlements import EFFECTIVE_SETTLEMENT

# Batch lifecycle: a batch is opened as in_progress and settles into completed
# within the same transaction — reconciliation only reads and records, never
# mutating orders, refunds or settlements.
IN_PROGRESS = "in_progress"
COMPLETED = "completed"

# Verification conclusions. The service invariants keep every normal flow
# balanced, but reconciliation reports a breach rather than enforcing one.
BALANCED = "balanced"
REFUNDS_EXCEED_RECEIVED = "refund_total_exceeds_received"

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
IN_PROGRESS_CONFLICT = "reconciliation_in_progress"

RECONCILE_OP = "reconcile"

# The HTTP server is single-process; an in-process per-order lock makes
# concurrent initiations of the same order fail fast with 409 instead of
# serializing them (which would let the second "succeed" as a stale read).
# The partial unique index ux_reconciliations_inprogress_per_order additionally
# guards the same invariant at the database level.
_LOCKS_GUARD = threading.Lock()
_ORDER_LOCKS: "defaultdict[tuple[str, str], threading.Lock]" = defaultdict(threading.Lock)


def _order_lock(tenant: str, order_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _ORDER_LOCKS[(tenant, order_id)]


def _row_to_body(row: sqlite3.Row) -> dict:
    return {
        "batch_id": row["batch_id"],
        "order_id": row["order_id"],
        "note": row["note"],
        "status": row["status"],
        "paid_cents": row["paid_cents"],
        "pending_refunds_cents": row["pending_refunds_cents"],
        "effective_refunds_cents": row["effective_refunds_cents"],
        "effective_settlements_cents": row["effective_settlements_cents"],
        "verified_balance_cents": row["verified_balance_cents"],
        "conclusion": row["conclusion"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def get(tenant: str, batch_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, batch_id, order_id, note, status, paid_cents, "
            "pending_refunds_cents, effective_refunds_cents, effective_settlements_cents, "
            "verified_balance_cents, conclusion, created_at, completed_at "
            "FROM reconciliation_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_body(row)


def execute(tenant: str, batch_id: str, order_id: str, note: str,
            fingerprint: str) -> Outcome:
    """Run a reconciliation batch idempotently.

    Totals are read from the order, its refunds and its effective settlements
    in one ``BEGIN IMMEDIATE`` transaction, so they are an atomic snapshot: a
    payment, refund acceptance/reversal or settlement advance/revoke landing
    concurrently is either fully before the snapshot or fully after it — the
    check never observes a partial write and never leaves an in-progress batch
    behind. Only one batch per order may run at a time; racing initiations get
    a distinguishable 409 without touching the first batch. A replayed request
    (same tenant/operation/batch/fingerprint) returns the original result, its
    original error included.
    """
    # Acquire the per-order gate BEFORE opening a write transaction: a loser
    # that lost the race must return 409 rather than queue on SQLite's write
    # lock and then "succeed" against a post-completion state.
    lock = _order_lock(tenant, order_id)
    held_lock = lock.acquire(blocking=False)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, RECONCILE_OP, batch_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # Missing and cross-tenant orders are indistinguishable.
            return record_and_finish(
                conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="reconciliation target order not found",
            )

        existing = conn.execute(
            "SELECT status FROM reconciliation_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="reconciliation batch already exists",
            )

        # Lost the race against another initiation of the same order. Record
        # the 409 (so this exact request replays it) without creating a batch.
        if not held_lock:
            return _in_progress_conflict(conn, tenant, batch_id, fingerprint)

        # --- atomic snapshot reads ---
        refund_totals = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status=? THEN amount_cents ELSE 0 END),0) AS pending_total, "
            "       COALESCE(SUM(CASE WHEN status=? THEN amount_cents ELSE 0 END),0) AS effective_total "
            "FROM refunds WHERE tenant=? AND order_id=?",
            (PENDING_REFUND, EFFECTIVE_REFUND, tenant, order_id),
        ).fetchone()
        pending_total = refund_totals["pending_total"]
        effective_refund_total = refund_totals["effective_total"]

        effective_settlement_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM settlements "
            "WHERE tenant=? AND order_id=? AND status=?",
            (tenant, order_id, EFFECTIVE_SETTLEMENT),
        ).fetchone()["s"]

        paid = order["paid_cents"]
        refund_total = pending_total + effective_refund_total
        verified_balance = paid - refund_total
        bounds_hold = refund_total <= paid <= order["amount_cents"]
        conclusion = BALANCED if verified_balance >= 0 and bounds_hold else REFUNDS_EXCEED_RECEIVED

        created_at = now_iso()
        conn.execute(
            "INSERT INTO reconciliation_batches(tenant, batch_id, order_id, note, status, "
            "paid_cents, pending_refunds_cents, effective_refunds_cents, "
            "effective_settlements_cents, verified_balance_cents, conclusion, "
            "created_at, completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant, batch_id, order_id, note, IN_PROGRESS,
             paid, pending_total, effective_refund_total, effective_settlement_total,
             verified_balance, conclusion, created_at, None),
        )

        completed_at = now_iso()
        conn.execute(
            "UPDATE reconciliation_batches SET status=?, paid_cents=?, pending_refunds_cents=?, "
            "effective_refunds_cents=?, effective_settlements_cents=?, verified_balance_cents=?, "
            "conclusion=?, completed_at=? WHERE tenant=? AND batch_id=?",
            (COMPLETED, paid, pending_total, effective_refund_total,
             effective_settlement_total, verified_balance, conclusion, completed_at,
             tenant, batch_id),
        )
        body = {
            "batch_id": batch_id,
            "order_id": order_id,
            "note": note,
            "status": COMPLETED,
            "paid_cents": paid,
            "pending_refunds_cents": pending_total,
            "effective_refunds_cents": effective_refund_total,
            "effective_settlements_cents": effective_settlement_total,
            "verified_balance_cents": verified_balance,
            "conclusion": conclusion,
            "created_at": created_at,
            "completed_at": completed_at,
        }
        store_outcome(conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    except Exception:
        # Any failure rolls the whole batch back: no in-progress row and no
        # partial conclusion survive; the caller retries with a fresh snapshot.
        conn.execute("ROLLBACK")
        raise
    finally:
        if held_lock:
            lock.release()
        conn.close()


def _in_progress_conflict(conn: sqlite3.Connection, tenant: str, batch_id: str,
                          fingerprint: str) -> Outcome:
    """Record the 409 so the same request replays it, then commit."""
    return record_and_finish(
        conn, tenant=tenant, operation=RECONCILE_OP, target_id=batch_id,
        fingerprint=fingerprint, status=409, code=IN_PROGRESS_CONFLICT,
        detail="another reconciliation for this order is already in progress",
    )
