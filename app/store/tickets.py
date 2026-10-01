import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome
from app.store.reconciliations import COMPLETED, MISMATCHED

# Ticket lifecycle:
#   pending -> processing -> resolved -> closed (terminal)
# pending/processing may also be resolved or closed directly. Every flip runs
# in a single immediate write transaction; a failure leaves existing data
# untouched. Tickets never mutate orders, refunds, settlements or
# reconciliation batches.
PENDING = "pending"
PROCESSING = "processing"
RESOLVED = "resolved"
CLOSED = "closed"

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INVALID_ISSUE = "invalid_issue"
ILLEGAL_TRANSITION = "illegal_transition"
RECONCILIATION_UNSETTLED = "reconciliation_unsettled"

ACCEPT_OP = "ticket_accept"
PROCESS_OP = "ticket_process"
RESOLVE_OP = "ticket_resolve"
CLOSE_OP = "ticket_close"


def _body(ticket_id: str, order_id: str, issue: str, status: str,
          created_at: str, updated_at: str) -> dict:
    return {
        "ticket_id": ticket_id,
        "order_id": order_id,
        "issue": issue,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _row_to_body(row: sqlite3.Row) -> dict:
    return _body(row["ticket_id"], row["order_id"], row["issue"], row["status"],
                 row["created_at"], row["updated_at"])


_COLUMNS = "tenant, ticket_id, order_id, issue, resolution_note, status, created_at, updated_at"


def get(tenant: str, ticket_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT ticket_id, order_id, issue, status, created_at, updated_at "
            "FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_body(row)


def accept(tenant: str, ticket_id: str, order_id: str, issue: str,
           fingerprint: str) -> Outcome:
    """Register a ticket in pending state for an order of the same tenant.

    A missing or cross-tenant order is indistinguishable (404, no existence
    leak). Re-accepting the same ticket id within a tenant conflicts without
    touching the existing ticket. A replayed request (same tenant/operation/
    ticket/fingerprint) returns the recorded first result, errors included.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, ACCEPT_OP, ticket_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="ticket target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="ticket already accepted",
            )

        if not isinstance(issue, str) or not issue:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=400, code=INVALID_ISSUE,
                detail="ticket issue must be a non-empty string",
            )

        ts = now_iso()
        conn.execute(
            "INSERT INTO tickets(tenant, ticket_id, order_id, issue, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, ticket_id, order_id, issue, PENDING, ts, ts),
        )
        body = {
            "ticket_id": ticket_id,
            "order_id": order_id,
            "issue": issue,
            "status": PENDING,
            "created_at": ts,
            "updated_at": ts,
        }
        store_outcome(conn, tenant=tenant, operation=ACCEPT_OP, target_id=ticket_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def process(tenant: str, ticket_id: str, fingerprint: str) -> Outcome:
    """Move a pending ticket to processing; processing is a no-op success.

    Resolved/closed tickets conflict without any state change. A process call
    that lands on an already-processing ticket performs no flip and changes no
    data, so concurrent starts can transition the ticket at most once.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, PROCESS_OP, ticket_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            f"SELECT {_COLUMNS} FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation=PROCESS_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="ticket not found",
            )

        if row["status"] in (RESOLVED, CLOSED):
            return record_and_finish(
                conn, tenant=tenant, operation=PROCESS_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=409, code=ILLEGAL_TRANSITION,
                detail=f"ticket in status {row['status']} cannot be processed",
            )

        if row["status"] == PENDING:
            ts = now_iso()
            conn.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE tenant=? AND ticket_id=? AND status=?",
                (PROCESSING, ts, tenant, ticket_id, PENDING),
            )
            status, updated_at = PROCESSING, ts
        else:
            status, updated_at = row["status"], row["updated_at"]

        body = _body(row["ticket_id"], row["order_id"], row["issue"], status,
                     row["created_at"], updated_at)
        store_outcome(conn, tenant=tenant, operation=PROCESS_OP, target_id=ticket_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()


def resolve(tenant: str, ticket_id: str, resolution_note: str,
            fingerprint: str) -> Outcome:
    """Resolve a pending/processing ticket, recording the resolution note.

    Closed tickets conflict. Resolution is additionally blocked while the
    order's latest completed reconciliation batch concludes mismatched: the
    order must be re-reconciled under a new batch to a balanced conclusion
    first. Closed tickets are exempt from that gate (they cannot be resolved
    regardless). Nothing changes on a rejected resolution.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, RESOLVE_OP, ticket_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            f"SELECT {_COLUMNS} FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation=RESOLVE_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="ticket not found",
            )

        if row["status"] == CLOSED:
            return record_and_finish(
                conn, tenant=tenant, operation=RESOLVE_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=409, code=ILLEGAL_TRANSITION,
                detail="ticket is closed and cannot be resolved",
            )
        if row["status"] == RESOLVED:
            return record_and_finish(
                conn, tenant=tenant, operation=RESOLVE_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=409, code=ILLEGAL_TRANSITION,
                detail="ticket already resolved",
            )

        latest = conn.execute(
            "SELECT conclusion FROM reconciliations "
            "WHERE tenant=? AND order_id=? AND status=? "
            "ORDER BY completed_at DESC, created_at DESC, rowid DESC LIMIT 1",
            (tenant, row["order_id"], COMPLETED),
        ).fetchone()
        if latest is not None and latest["conclusion"] == MISMATCHED:
            return record_and_finish(
                conn, tenant=tenant, operation=RESOLVE_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=409, code=RECONCILIATION_UNSETTLED,
                detail="order has a mismatched reconciliation batch; "
                       "re-reconcile under a new batch to a balanced conclusion first",
            )

        ts = now_iso()
        conn.execute(
            "UPDATE tickets SET status=?, resolution_note=?, updated_at=? "
            "WHERE tenant=? AND ticket_id=? AND status IN (?,?)",
            (RESOLVED, resolution_note, ts, tenant, ticket_id, PENDING, PROCESSING),
        )
        body = _body(row["ticket_id"], row["order_id"], row["issue"], RESOLVED,
                     row["created_at"], ts)
        store_outcome(conn, tenant=tenant, operation=RESOLVE_OP, target_id=ticket_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()


def close(tenant: str, ticket_id: str, fingerprint: str) -> Outcome:
    """Close a pending/processing/resolved ticket; closed is terminal."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, CLOSE_OP, ticket_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            f"SELECT {_COLUMNS} FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation=CLOSE_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="ticket not found",
            )

        if row["status"] == CLOSED:
            return record_and_finish(
                conn, tenant=tenant, operation=CLOSE_OP, target_id=ticket_id,
                fingerprint=fingerprint, status=409, code=ILLEGAL_TRANSITION,
                detail="ticket already closed",
            )

        ts = now_iso()
        conn.execute(
            "UPDATE tickets SET status=?, updated_at=? "
            "WHERE tenant=? AND ticket_id=? AND status IN (?,?,?)",
            (CLOSED, ts, tenant, ticket_id, PENDING, PROCESSING, RESOLVED),
        )
        body = _body(row["ticket_id"], row["order_id"], row["issue"], CLOSED,
                     row["created_at"], ts)
        store_outcome(conn, tenant=tenant, operation=CLOSE_OP, target_id=ticket_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()
