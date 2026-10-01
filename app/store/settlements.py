import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome
from app.store.refunds import EFFECTIVE, PENDING, REVERSED

# Settlement lifecycle: pending -> effective (terminal).
PENDING_SETTLEMENT = "pending"
EFFECTIVE_SETTLEMENT = "effective"

# Distinguishable error classes (mirrors the refund store's convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INVALID_AMOUNT = "invalid_amount"
INVALID_REFUNDS = "invalid_refunds"
ALREADY_ADVANCED = "already_advanced"
REFUND_REVERSED = "refund_reversed"
REFUND_ALREADY_SETTLED = "refund_already_settled"
AMOUNT_MISMATCH = "amount_mismatch"

ACCEPT_OP = "settle_accept"
ADVANCE_OP = "settle_advance"


def _row_to_body(row: sqlite3.Row, refund_ids: list[str]) -> dict:
    return {
        "settlement_id": row["settlement_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "reason": row["reason"],
        "refund_ids": refund_ids,
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _load_refund_ids(conn: sqlite3.Connection, tenant: str, settlement_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT refund_id FROM settlement_refunds "
        "WHERE tenant=? AND settlement_id=? ORDER BY position",
        (tenant, settlement_id),
    ).fetchall()
    return [r["refund_id"] for r in rows]


def get(tenant: str, settlement_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, settlement_id, order_id, amount_cents, reason, status, created_at, updated_at "
            "FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if row is None:
            return None
        refund_ids = _load_refund_ids(conn, tenant, settlement_id)
    finally:
        conn.close()
    return _row_to_body(row, refund_ids)


def accept(tenant: str, settlement_id: str, order_id: str, amount_cents: int,
           reason: str, refund_ids: list[str], fingerprint: str) -> Outcome:
    """Accept a settlement in pending state, snapshotting its refund references.

    The target order must exist within the same tenant (cross-tenant and
    missing are indistinguishable, like the refund endpoints). Referenced
    refunds are only validated at advance time: accepting merely records the
    intended set.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, ACCEPT_OP, settlement_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=settlement_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="settlement target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=settlement_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="settlement already accepted",
            )

        if not isinstance(amount_cents, int) or amount_cents <= 0:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=settlement_id,
                fingerprint=fingerprint, status=400, code=INVALID_AMOUNT,
                detail="settlement amount must be a positive integer",
            )
        if not refund_ids or any(not isinstance(r, str) or not r for r in refund_ids) \
                or len(set(refund_ids)) != len(refund_ids):
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=settlement_id,
                fingerprint=fingerprint, status=400, code=INVALID_REFUNDS,
                detail="refund_ids must be a non-empty list of unique non-empty identifiers",
            )

        ts = now_iso()
        conn.execute(
            "INSERT INTO settlements(tenant, settlement_id, order_id, amount_cents, reason, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (tenant, settlement_id, order_id, amount_cents, reason,
             PENDING_SETTLEMENT, ts, ts),
        )
        conn.executemany(
            "INSERT INTO settlement_refunds(tenant, settlement_id, refund_id, position) "
            "VALUES(?,?,?,?)",
            [(tenant, settlement_id, refund_id, pos) for pos, refund_id in enumerate(refund_ids)],
        )
        body = {
            "settlement_id": settlement_id,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "reason": reason,
            "refund_ids": list(refund_ids),
            "status": PENDING_SETTLEMENT,
            "created_at": ts,
            "updated_at": ts,
        }
        store_outcome(conn, tenant=tenant, operation=ACCEPT_OP, target_id=settlement_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def advance(tenant: str, settlement_id: str, fingerprint: str) -> Outcome:
    """Atomically turn every referenced pending refund effective.

    Either all referenced pending refunds become effective and the settlement
    becomes effective, or nothing changes and a distinguishable failure is
    returned. Serialized via BEGIN IMMEDIATE; settlement_claims' primary key
    additionally guarantees a refund can never be settled twice.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, ADVANCE_OP, settlement_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            "SELECT order_id, amount_cents, reason, status, created_at, updated_at "
            "FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation=ADVANCE_OP, target_id=settlement_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="settlement not found",
            )

        if row["status"] == EFFECTIVE_SETTLEMENT:
            return record_and_finish(
                conn, tenant=tenant, operation=ADVANCE_OP, target_id=settlement_id,
                fingerprint=fingerprint, status=409, code=ALREADY_ADVANCED,
                detail="settlement already advanced",
            )

        refund_ids = _load_refund_ids(conn, tenant, settlement_id)

        # Validate every reference first; the first breach wins and the whole
        # advance leaves existing data untouched.
        refund_rows: list[sqlite3.Row] = []
        for refund_id in refund_ids:
            refund = conn.execute(
                "SELECT order_id, amount_cents, status FROM refunds WHERE tenant=? AND refund_id=?",
                (tenant, refund_id),
            ).fetchone()
            # A missing id, or a refund of a different order, is "not found"
            # from this settlement's point of view.
            if refund is None or refund["order_id"] != row["order_id"]:
                return _fail(conn, tenant, settlement_id, fingerprint, 404, NOT_FOUND,
                             "referenced refund not found")

            claim = conn.execute(
                "SELECT settlement_id FROM settlement_claims WHERE tenant=? AND refund_id=?",
                (tenant, refund_id),
            ).fetchone()
            if claim is not None and claim["settlement_id"] != settlement_id:
                return _fail(conn, tenant, settlement_id, fingerprint, 409,
                             REFUND_ALREADY_SETTLED,
                             "referenced refund already settled by another settlement")

            if refund["status"] == REVERSED:
                return _fail(conn, tenant, settlement_id, fingerprint, 409,
                             REFUND_REVERSED, "referenced refund is reversed")
            if refund["status"] != PENDING:
                # effective without an own claim cannot arise through the API;
                # only a pending refund may be advanced.
                return _fail(conn, tenant, settlement_id, fingerprint, 409,
                             CONFLICT, "referenced refund is not pending")
            refund_rows.append(refund)

        total = sum(r["amount_cents"] for r in refund_rows)
        if total != row["amount_cents"]:
            return _fail(conn, tenant, settlement_id, fingerprint, 409,
                         AMOUNT_MISMATCH,
                         "settlement amount does not match referenced refunds total")

        # All checks passed: claims first (hard uniqueness guard), then flips,
        # then the settlement itself — all in the same transaction.
        ts = now_iso()
        conn.executemany(
            "INSERT INTO settlement_claims(tenant, refund_id, settlement_id, created_at) "
            "VALUES(?,?,?,?)",
            [(tenant, refund_id, settlement_id, ts) for refund_id in refund_ids],
        )
        conn.executemany(
            "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=? AND status=?",
            [(EFFECTIVE, tenant, refund_id, PENDING) for refund_id in refund_ids],
        )
        conn.execute(
            "UPDATE settlements SET status=?, updated_at=? WHERE tenant=? AND settlement_id=?",
            (EFFECTIVE_SETTLEMENT, ts, tenant, settlement_id),
        )
        body = {
            "settlement_id": settlement_id,
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "reason": row["reason"],
            "refund_ids": refund_ids,
            "status": EFFECTIVE_SETTLEMENT,
            "created_at": row["created_at"],
            "updated_at": ts,
        }
        store_outcome(conn, tenant=tenant, operation=ADVANCE_OP, target_id=settlement_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()


def _fail(conn: sqlite3.Connection, tenant: str, settlement_id: str, fingerprint: str,
          status: int, code: str, detail: str) -> Outcome:
    return record_and_finish(
        conn, tenant=tenant, operation=ADVANCE_OP, target_id=settlement_id,
        fingerprint=fingerprint, status=status, code=code, detail=detail,
    )
