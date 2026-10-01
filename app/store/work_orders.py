import sqlite3
from collections.abc import Callable

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome
from app.store.reconciliations import COMPLETED, MISMATCHED

# Work order lifecycle:
#   pending -> in_progress -> resolved -> closed
# pending and in_progress may also skip straight to closed.
# closed is terminal.
PENDING = "pending"
IN_PROGRESS = "in_progress"
RESOLVED = "resolved"
CLOSED = "closed"

# Distinguishable error classes (mirror the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
ILLEGAL_STATE = "illegal_state_transition"
RECONCILIATION_UNSETTLED = "reconciliation_unsettled"

ACCEPT_OP = "work_order_accept"
PROCESS_OP = "work_order_process"
RESOLVE_OP = "work_order_resolve"
CLOSE_OP = "work_order_close"


def _row_to_body(row: sqlite3.Row) -> dict:
    return {
        "work_order_id": row["work_order_id"],
        "order_id": row["order_id"],
        "issue": row["issue"],
        "resolution": row["resolution"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get(tenant: str, work_order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, work_order_id, order_id, issue, resolution, status, created_at, updated_at "
            "FROM work_orders WHERE tenant=? AND work_order_id=?",
            (tenant, work_order_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_body(row)


def accept(tenant: str, work_order_id: str, order_id: str, issue: str,
           fingerprint: str) -> Outcome:
    """Register a work order against an existing same-tenant order.

    Missing and cross-tenant target orders are indistinguishable (404), like
    the refund and settlement endpoints. A replayed request returns the
    recorded first result, errors included; a duplicate work order id under a
    different fingerprint conflicts and leaves the existing work order alone.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, ACCEPT_OP, work_order_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=work_order_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="work order target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM work_orders WHERE tenant=? AND work_order_id=?",
            (tenant, work_order_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=work_order_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="work order already accepted",
            )

        ts = now_iso()
        conn.execute(
            "INSERT INTO work_orders(tenant, work_order_id, order_id, issue, resolution, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (tenant, work_order_id, order_id, issue, "", PENDING, ts, ts),
        )
        body = {
            "work_order_id": work_order_id,
            "order_id": order_id,
            "issue": issue,
            "resolution": "",
            "status": PENDING,
            "created_at": ts,
            "updated_at": ts,
        }
        store_outcome(conn, tenant=tenant, operation=ACCEPT_OP, target_id=work_order_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def _transition(tenant: str, work_order_id: str, fingerprint: str, *,
                operation: str, allowed_from: set[str], new_status: str,
                conflict_code: str, conflict_detail: str,
                resolution: str | None = None,
                extra_check: Callable[[sqlite3.Connection, str, str],
                                      tuple[str, str] | None] | None = None) -> Outcome:
    """Shared state-flip machinery: validate, flip, record — one transaction."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, operation, work_order_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            "SELECT order_id, issue, resolution, status, created_at FROM work_orders "
            "WHERE tenant=? AND work_order_id=?",
            (tenant, work_order_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation=operation, target_id=work_order_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="work order not found",
            )

        if row["status"] not in allowed_from:
            return record_and_finish(
                conn, tenant=tenant, operation=operation, target_id=work_order_id,
                fingerprint=fingerprint, status=409, code=conflict_code,
                detail=conflict_detail,
            )

        if extra_check is not None:
            blocked = extra_check(conn, tenant, row["order_id"])
            if blocked is not None:
                code, detail = blocked
                return record_and_finish(
                    conn, tenant=tenant, operation=operation, target_id=work_order_id,
                    fingerprint=fingerprint, status=409, code=code, detail=detail,
                )

        ts = now_iso()
        if resolution is not None:
            conn.execute(
                "UPDATE work_orders SET status=?, resolution=?, updated_at=? "
                "WHERE tenant=? AND work_order_id=?",
                (new_status, resolution, ts, tenant, work_order_id),
            )
        else:
            conn.execute(
                "UPDATE work_orders SET status=?, updated_at=? WHERE tenant=? AND work_order_id=?",
                (new_status, ts, tenant, work_order_id),
            )
        body = {
            "work_order_id": work_order_id,
            "order_id": row["order_id"],
            "issue": row["issue"],
            "resolution": resolution if resolution is not None else row["resolution"],
            "status": new_status,
            "created_at": row["created_at"],
            "updated_at": ts,
        }
        store_outcome(conn, tenant=tenant, operation=operation, target_id=work_order_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()


def process(tenant: str, work_order_id: str, fingerprint: str) -> Outcome:
    """pending/in_progress -> in_progress; resolved/closed are rejected."""
    return _transition(
        tenant, work_order_id, fingerprint,
        operation=PROCESS_OP, allowed_from={PENDING, IN_PROGRESS},
        new_status=IN_PROGRESS,
        conflict_code=ILLEGAL_STATE,
        conflict_detail="work order cannot be processed from its current status",
    )


def _unsettled_reconciliation(conn: sqlite3.Connection, tenant: str,
                              order_id: str) -> tuple[str, str] | None:
    """Block resolution while the order's latest completed batch is mismatched.

    A newer completed batch with a balanced conclusion re-opens the door to
    resolving; historical mismatched batches are kept for the audit trail and
    do not block once superseded.
    """
    row = conn.execute(
        "SELECT conclusion FROM reconciliations "
        "WHERE tenant=? AND order_id=? AND status=? "
        "ORDER BY completed_at DESC, rowid DESC LIMIT 1",
        (tenant, order_id, COMPLETED),
    ).fetchone()
    if row is not None and row["conclusion"] == MISMATCHED:
        return (RECONCILIATION_UNSETTLED,
                "order has a mismatched completed reconciliation; re-reconcile as balanced first")
    return None


def resolve(tenant: str, work_order_id: str, resolution: str,
            fingerprint: str) -> Outcome:
    """pending/in_progress -> resolved with a resolution note.

    Resolving is refused while the order's latest completed reconciliation
    batch concluded mismatched. Closed work orders are exempt — they are
    refused instead by the state-machine guard below.
    """
    return _transition(
        tenant, work_order_id, fingerprint,
        operation=RESOLVE_OP, allowed_from={PENDING, IN_PROGRESS},
        new_status=RESOLVED, resolution=resolution,
        conflict_code=ILLEGAL_STATE,
        conflict_detail="work order cannot be resolved from its current status",
        extra_check=_unsettled_reconciliation,
    )


def close(tenant: str, work_order_id: str, fingerprint: str) -> Outcome:
    """pending/in_progress/resolved -> closed (terminal); re-close conflicts."""
    return _transition(
        tenant, work_order_id, fingerprint,
        operation=CLOSE_OP, allowed_from={PENDING, IN_PROGRESS, RESOLVED},
        new_status=CLOSED,
        conflict_code=ILLEGAL_STATE,
        conflict_detail="work order already closed",
    )
