import base64
import json
import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome

# Stock movements are immutable records of inbound (in) / outbound (out)
# quantities against an accepted order. They act only on themselves: accepting,
# reversing or reading one never changes orders, refunds, settlements,
# reconciliation batches, tickets or import tasks.
IN = "in"
OUT = "out"
DIRECTIONS = (IN, OUT)

# Lifecycle: accepted -> reversed (terminal). A reversed movement keeps its
# original direction/quantity/created_at but counts as 0 in quantity summaries:
# net per (tenant, order) = SUM(quantity of in, accepted)
#                           - SUM(quantity of out, accepted).
ACCEPTED = "accepted"
REVERSED = "reversed"
STATUSES = (ACCEPTED, REVERSED)

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INVALID_PARAM = "invalid_param"

ACCEPT_OP = "stock_movement_accept"
REVERSE_OP = "stock_movement_reverse"

# Audit-trail actions recorded in stock_movement_events.
ACCEPT_ACTION = "accept"
REVERSE_ACTION = "reverse"

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500


class InvalidCursor(Exception):
    """The pagination cursor is malformed or was not issued by this service."""


class OrderSummaryNotFound(Exception):
    """The requested order has no stock movements (or does not exist here).

    Raised for the single-order summary lookup; the two cases are deliberately
    indistinguishable so the response never leaks whether the order exists.
    """


def _record_event(conn: sqlite3.Connection, tenant: str, movement_id: str,
                  action: str, status: str, direction: str, quantity: int,
                  occurred_at: str) -> None:
    """Append one audit event inside the caller's transaction.

    ``seq`` is the next consecutive number within the movement (1-based) and is
    never rewritten once stored. Called only on the success path of accept and
    reverse, so replays, rejections and losing concurrent writers leave no
    event behind.
    """
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM stock_movement_events "
        "WHERE tenant=? AND movement_id=?",
        (tenant, movement_id),
    ).fetchone()["next_seq"]
    conn.execute(
        "INSERT INTO stock_movement_events(tenant, movement_id, seq, action, status, "
        "direction, quantity, occurred_at) VALUES(?,?,?,?,?,?,?,?)",
        (tenant, movement_id, seq, action, status, direction, quantity, occurred_at),
    )


def _row_to_body(row: sqlite3.Row) -> dict:
    return {
        "movement_id": row["movement_id"],
        "order_id": row["order_id"],
        "direction": row["direction"],
        "quantity": row["quantity"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


_SELECT = (
    "SELECT movement_id, order_id, direction, quantity, status, created_at "
    "FROM stock_movements"
)


def get(tenant: str, movement_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            f"{_SELECT} WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_body(row)


def events(tenant: str, movement_id: str) -> list[dict] | None:
    """Return the audit trail of one movement, or ``None`` if it does not exist.

    A missing or cross-tenant movement is indistinguishable (``None``, no
    existence leak). Events are ordered by occurred_at ASC then seq ASC; seq is
    consecutive from 1 within the movement and immutable once written, so the
    accept event always precedes the reverse event (if any). Each entry carries
    the action (``accept``/``reverse``), the movement status right after the
    action (``accepted``/``reversed``) and the direction/quantity as accepted;
    a reverse entry keeps the accepted direction and quantity verbatim.
    Read-only: never changes any document.
    """
    conn = connect()
    try:
        movement = conn.execute(
            "SELECT 1 FROM stock_movements WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()
        if movement is None:
            return None
        rows = conn.execute(
            "SELECT seq, action, status, direction, quantity, occurred_at "
            "FROM stock_movement_events WHERE tenant=? AND movement_id=? "
            "ORDER BY occurred_at ASC, seq ASC",
            (tenant, movement_id),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "seq": row["seq"],
            "action": row["action"],
            "status": row["status"],
            "direction": row["direction"],
            "quantity": row["quantity"],
            "occurred_at": row["occurred_at"],
        }
        for row in rows
    ]


def accept(tenant: str, movement_id: str, order_id: str, direction: str,
           quantity: int, fingerprint: str) -> Outcome:
    """Accept a stock movement for an order of the same tenant.

    A missing or cross-tenant order is indistinguishable (404, no existence
    leak). Re-accepting the same movement id within a tenant conflicts without
    touching the existing movement; direction and quantity are stored verbatim
    and never change. A replayed request (same tenant/operation/movement/
    fingerprint) returns the recorded first result, errors included. A
    successful accept appends the movement's ``accept`` audit event in the
    same transaction; failures, rejections and replays append nothing.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, ACCEPT_OP, movement_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="stock movement target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM stock_movements WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="stock movement already accepted",
            )

        created_at = now_iso()
        conn.execute(
            "INSERT INTO stock_movements(tenant, movement_id, order_id, direction, quantity, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, movement_id, order_id, direction, quantity, ACCEPTED, created_at),
        )
        _record_event(conn, tenant, movement_id, ACCEPT_ACTION, ACCEPTED,
                      direction, quantity, created_at)
        body = {
            "movement_id": movement_id,
            "order_id": order_id,
            "direction": direction,
            "quantity": quantity,
            "status": ACCEPTED,
            "created_at": created_at,
        }
        store_outcome(conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def reverse(tenant: str, movement_id: str, fingerprint: str) -> Outcome:
    """Reverse an accepted stock movement idempotently.

    The movement enters the terminal ``reversed`` state in a single atomic
    transaction; its direction/quantity/created_at are preserved and it stops
    counting toward quantity summaries. A missing or cross-tenant movement is
    indistinguishable (404, no existence leak); reversing an already reversed
    movement conflicts (409) without a second state flip. A replayed request
    (same tenant/operation/movement/fingerprint) returns the recorded first
    result, including a recorded 404/409, even if the movement later changes.
    A successful reverse appends the movement's ``reverse`` audit event
    (keeping the accepted direction/quantity) in the same transaction;
    failures, rejections and replays append nothing.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = lookup_replay(conn, tenant, REVERSE_OP, movement_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            f"{_SELECT} WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()
        if row is None:
            return record_and_finish(
                conn, tenant=tenant, operation=REVERSE_OP, target_id=movement_id,
                fingerprint=fingerprint, status=404, code=NOT_FOUND,
                detail="stock movement not found",
            )

        if row["status"] == REVERSED:
            return record_and_finish(
                conn, tenant=tenant, operation=REVERSE_OP, target_id=movement_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="stock movement already reversed",
            )

        conn.execute(
            "UPDATE stock_movements SET status=? WHERE tenant=? AND movement_id=?",
            (REVERSED, tenant, movement_id),
        )
        _record_event(conn, tenant, movement_id, REVERSE_ACTION, REVERSED,
                      row["direction"], row["quantity"], now_iso())
        body = {
            "movement_id": movement_id,
            "order_id": row["order_id"],
            "direction": row["direction"],
            "quantity": row["quantity"],
            "status": REVERSED,
            "created_at": row["created_at"],
        }
        store_outcome(conn, tenant=tenant, operation=REVERSE_OP, target_id=movement_id,
                      fingerprint=fingerprint, code="ok", status=200, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()


def encode_cursor(created_at: str, movement_id: str) -> str:
    raw = json.dumps({"created_at": created_at, "movement_id": movement_id},
                     separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        created_at = payload["created_at"]
        movement_id = payload["movement_id"]
    except Exception as error:
        raise InvalidCursor("cursor is invalid") from error
    if not isinstance(created_at, str) or not isinstance(movement_id, str) or not created_at:
        raise InvalidCursor("cursor is invalid")
    return created_at, movement_id


def search(tenant: str, *, order_id: str | None = None, direction: str | None = None,
           quantity_min: int | None = None, quantity_max: int | None = None,
           created_from: str | None = None, created_to: str | None = None,
           status: str | None = None, include_reversed: bool = False,
           cursor: str | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """List a tenant's movements under an arbitrary AND-combination of filters.

    Ranges are inclusive. Ordering is created_at ASC then movement_id ASC, which
    is independent of insertion order and stable across repeated queries.
    Keyset pagination positions the cursor at the last row of the previous page
    in exactly that ordering, so pages never repeat or skip a row and the union
    of pages in any order equals the full filtered result set — including pages
    of a result set that mixes accepted and reversed movements.

    Status visibility: by default only ``accepted`` movements are returned;
    ``include_reversed=True`` returns every status; an explicit ``status`` does
    an exact match. Supplying both ``status`` and ``include_reversed`` is the
    caller's mistake and must be rejected (400) before calling this function.
    """
    clauses = ["tenant=?"]
    params: list[object] = [tenant]
    if order_id is not None:
        clauses.append("order_id=?")
        params.append(order_id)
    if direction is not None:
        clauses.append("direction=?")
        params.append(direction)
    if quantity_min is not None:
        clauses.append("quantity>=?")
        params.append(quantity_min)
    if quantity_max is not None:
        clauses.append("quantity<=?")
        params.append(quantity_max)
    if created_from is not None:
        clauses.append("created_at>=?")
        params.append(created_from)
    if created_to is not None:
        clauses.append("created_at<=?")
        params.append(created_to)
    if not include_reversed:
        clauses.append("status=?")
        params.append(status if status is not None else ACCEPTED)
    if cursor is not None:
        cur_created_at, cur_movement_id = decode_cursor(cursor)
        clauses.append(
            "(created_at>? OR (created_at=? AND movement_id>?))"
        )
        params.extend((cur_created_at, cur_created_at, cur_movement_id))

    sql = (
        f"{_SELECT} WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at ASC, movement_id ASC LIMIT ?"
    )

    conn = connect()
    try:
        rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    finally:
        conn.close()

    has_more = len(rows) > limit
    page = [_row_to_body(row) for row in rows[:limit]]
    next_cursor = encode_cursor(page[-1]["created_at"], page[-1]["movement_id"]) if has_more else None
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
    if not isinstance(order_id, str) or not order_id:
        raise InvalidCursor("cursor is invalid")
    return order_id


def summary(tenant: str, *, order_id: str | None = None, cursor: str | None = None,
            limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """Aggregate this tenant's movements into a per-order net-quantity summary.

    ``net_quantity`` per order = SUM(quantity of in, accepted)
    - SUM(quantity of out, accepted); reversed movements count 0 regardless of
    their original direction, so the summary always equals the per-movement
    tally after any accept/reverse/replay sequence. With ``order_id`` the
    single matching row is returned, or ``OrderSummaryNotFound`` when the order
    has no movements (indistinguishable from a missing/cross-tenant order).
    Without ``order_id`` every order that has movements is listed, ordered by
    order_id ASC — independent of insertion order and stable across repeated
    queries. Keyset pagination positions the cursor at the last order_id of
    the previous page and continues strictly after it, so pages never repeat
    or skip an order and the union of pages in any order equals the full
    result set. Read-only: never changes any document.
    """
    clauses = ["tenant=?"]
    params: list[object] = [tenant]
    if order_id is not None:
        clauses.append("order_id=?")
        params.append(order_id)
    elif cursor is not None:
        clauses.append("order_id>?")
        params.append(decode_summary_cursor(cursor))

    sql = (
        "SELECT order_id, "
        "SUM(CASE WHEN status='accepted' AND direction='in' THEN quantity "
        "WHEN status='accepted' AND direction='out' THEN -quantity "
        "ELSE 0 END) AS net_quantity "
        f"FROM stock_movements WHERE {' AND '.join(clauses)} "
        "GROUP BY order_id ORDER BY order_id ASC LIMIT ?"
    )

    conn = connect()
    try:
        rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    finally:
        conn.close()

    if order_id is not None:
        if not rows:
            raise OrderSummaryNotFound("order has no stock movement summary")
        row = rows[0]
        return {"items": [{"order_id": row["order_id"], "net_quantity": row["net_quantity"]}],
                "next_cursor": None}

    has_more = len(rows) > limit
    page = [{"order_id": row["order_id"], "net_quantity": row["net_quantity"]}
            for row in rows[:limit]]
    next_cursor = encode_summary_cursor(page[-1]["order_id"]) if has_more else None
    return {"items": page, "next_cursor": next_cursor}
