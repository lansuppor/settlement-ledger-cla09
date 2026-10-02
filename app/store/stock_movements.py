import base64
import json
import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome

# Stock movements are immutable records of inbound (in) / outbound (out)
# quantities against an accepted order. They act only on themselves: accepting
# or reading one never changes orders, refunds, settlements, reconciliation
# batches, tickets or import tasks.
IN = "in"
OUT = "out"
DIRECTIONS = (IN, OUT)

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INVALID_PARAM = "invalid_param"

ACCEPT_OP = "stock_movement_accept"

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500


class InvalidCursor(Exception):
    """The pagination cursor is malformed or was not issued by this service."""


def _row_to_body(row: sqlite3.Row) -> dict:
    return {
        "movement_id": row["movement_id"],
        "order_id": row["order_id"],
        "direction": row["direction"],
        "quantity": row["quantity"],
        "created_at": row["created_at"],
    }


_SELECT = (
    "SELECT movement_id, order_id, direction, quantity, created_at "
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


def accept(tenant: str, movement_id: str, order_id: str, direction: str,
           quantity: int, fingerprint: str) -> Outcome:
    """Accept a stock movement for an order of the same tenant.

    A missing or cross-tenant order is indistinguishable (404, no existence
    leak). Re-accepting the same movement id within a tenant conflicts without
    touching the existing movement; direction and quantity are stored verbatim
    and never change. A replayed request (same tenant/operation/movement/
    fingerprint) returns the recorded first result, errors included.
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
            "INSERT INTO stock_movements(tenant, movement_id, order_id, direction, quantity, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (tenant, movement_id, order_id, direction, quantity, created_at),
        )
        body = {
            "movement_id": movement_id,
            "order_id": order_id,
            "direction": direction,
            "quantity": quantity,
            "created_at": created_at,
        }
        store_outcome(conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
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
           cursor: str | None = None, limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """List a tenant's movements under an arbitrary AND-combination of filters.

    Ranges are inclusive. Ordering is created_at ASC then movement_id ASC, which
    is independent of insertion order and stable across repeated queries.
    Keyset pagination positions the cursor at the last row of the previous page
    in exactly that ordering, so pages never repeat or skip a row and the union
    of pages in any order equals the full filtered result set.
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
