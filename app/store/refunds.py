import base64
import json
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

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500


class InvalidCursor(Exception):
    """The pagination cursor is malformed or was not issued by this service."""


class OrderSummaryNotFound(Exception):
    """The requested order has no refunds (or does not exist here).

    Raised for the single-order summary lookup; the two cases are deliberately
    indistinguishable so the response never leaks whether the order exists.
    """


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


_SELECT = (
    "SELECT refund_id, order_id, amount_cents, reason, status, created_at "
    "FROM refunds"
)


def encode_cursor(created_at: str, refund_id: str) -> str:
    raw = json.dumps({"created_at": created_at, "refund_id": refund_id},
                     separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        created_at = payload["created_at"]
        refund_id = payload["refund_id"]
    except Exception as error:
        raise InvalidCursor("cursor is invalid") from error
    if not isinstance(created_at, str) or not isinstance(refund_id, str) or not created_at:
        raise InvalidCursor("cursor is invalid")
    return created_at, refund_id


def search(tenant: str, *, order_id: str | None = None, status: str | None = None,
           amount_min: int | None = None, amount_max: int | None = None,
           created_from: str | None = None, created_to: str | None = None,
           include_reversed: bool = False,
           cursor: str | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """List a tenant's refunds under an arbitrary AND-combination of filters.

    Ranges are inclusive. Ordering is created_at ASC then refund_id ASC, which
    is independent of insertion order and stable across repeated queries.
    Keyset pagination positions the cursor at the last row of the previous page
    in exactly that ordering, so pages never repeat or skip a row and the union
    of pages in any order equals the full filtered result set — including pages
    of a result set that mixes pending, effective and reversed refunds.

    Status visibility: by default only ``pending`` and ``effective`` refunds
    are returned; ``include_reversed=True`` returns every status; an explicit
    ``status`` does an exact match. Supplying both ``status`` and
    ``include_reversed`` is the caller's mistake and must be rejected (400)
    before calling this function. Read-only: never changes any document.
    """
    clauses = ["tenant=?"]
    params: list[object] = [tenant]
    if order_id is not None:
        clauses.append("order_id=?")
        params.append(order_id)
    if amount_min is not None:
        clauses.append("amount_cents>=?")
        params.append(amount_min)
    if amount_max is not None:
        clauses.append("amount_cents<=?")
        params.append(amount_max)
    if created_from is not None:
        clauses.append("created_at>=?")
        params.append(created_from)
    if created_to is not None:
        clauses.append("created_at<=?")
        params.append(created_to)
    if not include_reversed:
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        else:
            clauses.append("status IN (?,?)")
            params.extend((PENDING, EFFECTIVE))
    if cursor is not None:
        cur_created_at, cur_refund_id = decode_cursor(cursor)
        clauses.append(
            "(created_at>? OR (created_at=? AND refund_id>?))"
        )
        params.extend((cur_created_at, cur_created_at, cur_refund_id))

    sql = (
        f"{_SELECT} WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at ASC, refund_id ASC LIMIT ?"
    )

    conn = connect()
    try:
        rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    finally:
        conn.close()

    has_more = len(rows) > limit
    page = [_row_to_dict(row) for row in rows[:limit]]
    next_cursor = encode_cursor(page[-1]["created_at"], page[-1]["refund_id"]) if has_more else None
    return {"items": page, "next_cursor": next_cursor}


def encode_summary_cursor(order_id: str) -> str:
    raw = json.dumps({"order_id": order_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_summary_cursor(cursor: str) -> str:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        order_id = payload["order_id"]
    except Exception as error:
        raise InvalidCursor("cursor is invalid") from error
    if not isinstance(order_id, str) or not order_id or set(payload) != {"order_id"}:
        raise InvalidCursor("cursor is invalid")
    return order_id


def summary(tenant: str, *, order_id: str | None = None, cursor: str | None = None,
            limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """Aggregate this tenant's refunds into a per-order refundable-amount summary.

    ``refundable_cents`` per order = the order's paid_cents - SUM(amount_cents)
    of its pending/effective refunds; reversed refunds occupy no quota, so the
    summary always equals the per-refund tally after any accept/reverse/
    settle-advance/settle-revoke/replay sequence. With ``order_id`` the single
    matching row is returned, or ``OrderSummaryNotFound`` when the order has no
    refunds (indistinguishable from a missing/cross-tenant order). Without
    ``order_id`` every order that has refunds is listed, ordered by order_id
    ASC — independent of insertion order and stable across repeated queries.
    Keyset pagination positions the cursor at the last order_id of the previous
    page and continues strictly after it, so pages never repeat or skip an
    order and the union of pages in any order equals the full result set.
    Read-only: never changes any document.
    """
    clauses = ["r.tenant=?"]
    params: list[object] = [tenant]
    if order_id is not None:
        clauses.append("r.order_id=?")
        params.append(order_id)
    elif cursor is not None:
        clauses.append("r.order_id>?")
        params.append(decode_summary_cursor(cursor))

    sql = (
        "SELECT r.order_id AS order_id, "
        "o.paid_cents - SUM(CASE WHEN r.status IN (?,?) THEN r.amount_cents ELSE 0 END) "
        "AS refundable_cents "
        "FROM refunds r JOIN orders o ON o.tenant=r.tenant AND o.order_id=r.order_id "
        f"WHERE {' AND '.join(clauses)} "
        "GROUP BY r.order_id, o.paid_cents ORDER BY r.order_id ASC LIMIT ?"
    )

    conn = connect()
    try:
        rows = conn.execute(sql, (PENDING, EFFECTIVE, *params, limit + 1)).fetchall()
    finally:
        conn.close()

    if order_id is not None:
        if not rows:
            raise OrderSummaryNotFound("order has no refund summary")
        row = rows[0]
        return {"items": [{"order_id": row["order_id"],
                           "refundable_cents": row["refundable_cents"]}],
                "next_cursor": None}

    has_more = len(rows) > limit
    page = [{"order_id": row["order_id"], "refundable_cents": row["refundable_cents"]}
            for row in rows[:limit]]
    next_cursor = encode_summary_cursor(page[-1]["order_id"]) if has_more else None
    return {"items": page, "next_cursor": next_cursor}
