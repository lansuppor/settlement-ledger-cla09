import base64
import binascii
import json
import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome

# Stock movement directions: in (入库) / out (出库). Quantity is a positive
# integer kept verbatim after acceptance. Movements never mutate orders,
# refunds, settlements, reconciliation batches, tickets or import tasks.
IN = "in"
OUT = "out"
DIRECTIONS = (IN, OUT)

# Distinguishable error classes (mirrors the other stores' convention).
NOT_FOUND = "not_found"
CONFLICT = "conflict"
INVALID_DIRECTION = "invalid_direction"
INVALID_QUANTITY = "invalid_quantity"

ACCEPT_OP = "movement_accept"

# Stable pagination: order by (created_at, movement_id) ascending; the cursor
# points at the last item of the previous page within that ordering.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class InvalidCursor(ValueError):
    """Raised when a pagination cursor cannot be decoded."""


def _body(movement_id: str, order_id: str, direction: str, quantity: int,
          created_at: str) -> dict:
    return {
        "movement_id": movement_id,
        "order_id": order_id,
        "direction": direction,
        "quantity": quantity,
        "created_at": created_at,
    }


def _row_to_body(row: sqlite3.Row) -> dict:
    return _body(row["movement_id"], row["order_id"], row["direction"],
                 row["quantity"], row["created_at"])


def get(tenant: str, movement_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT movement_id, order_id, direction, quantity, created_at "
            "FROM stock_movements WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _row_to_body(row)


def accept(tenant: str, movement_id: str, order_id: str, direction: str,
           quantity: int, fingerprint: str) -> Outcome:
    """Accept a stock movement idempotently.

    A missing or cross-tenant order is indistinguishable (404, no existence
    leak). Re-accepting the same movement id within a tenant conflicts without
    touching the existing movement. A replayed request (same tenant/operation/
    movement/fingerprint) returns the recorded first result, errors included.
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
                detail="movement target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM stock_movements WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()
        if existing is not None:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                fingerprint=fingerprint, status=409, code=CONFLICT,
                detail="movement already accepted",
            )

        if direction not in DIRECTIONS:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                fingerprint=fingerprint, status=400, code=INVALID_DIRECTION,
                detail="direction must be 'in' or 'out'",
            )
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            return record_and_finish(
                conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                fingerprint=fingerprint, status=400, code=INVALID_QUANTITY,
                detail="quantity must be a positive integer",
            )

        created_at = now_iso()
        conn.execute(
            "INSERT INTO stock_movements(tenant, movement_id, order_id, direction, quantity, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (tenant, movement_id, order_id, direction, quantity, created_at),
        )
        body = _body(movement_id, order_id, direction, quantity, created_at)
        store_outcome(conn, tenant=tenant, operation=ACCEPT_OP, target_id=movement_id,
                      fingerprint=fingerprint, code="ok", status=201, detail="",
                      body=body)
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def _encode_cursor(created_at: str, movement_id: str) -> str:
    raw = json.dumps({"c": created_at, "m": movement_id}, ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        created_at, movement_id = payload["c"], payload["m"]
    except (ValueError, binascii.Error, KeyError, TypeError, UnicodeDecodeError) as error:
        raise InvalidCursor("cursor is not valid") from error
    if not isinstance(created_at, str) or not isinstance(movement_id, str):
        raise InvalidCursor("cursor is not valid")
    return created_at, movement_id


def search(tenant: str, *, order_id: str | None = None,
           direction: str | None = None,
           quantity_min: int | None = None, quantity_max: int | None = None,
           created_from: str | None = None, created_to: str | None = None,
           limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None) -> dict:
    """Search movements of one tenant with AND-combined, endpoint-inclusive filters.

    Results are ordered by created_at ascending, ties broken by movement_id
    ascending, so the ordering is stable regardless of insertion order or
    repeated queries. The cursor resumes strictly after the last item of the
    previous page: pages neither repeat nor skip items, and the union of all
    pages equals the full result set. A next_cursor is returned only when more
    items remain beyond this page.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    clauses = ["tenant=?"]
    params: list = [tenant]
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
        last_created, last_id = _decode_cursor(cursor)
        clauses.append("(created_at>? OR (created_at=? AND movement_id>?))")
        params.extend([last_created, last_created, last_id])

    where = " AND ".join(clauses)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT movement_id, order_id, direction, quantity, created_at "
            f"FROM stock_movements WHERE {where} "
            "ORDER BY created_at ASC, movement_id ASC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    finally:
        conn.close()

    has_more = len(rows) > limit
    page = rows[:limit]
    result = {"items": [_row_to_body(row) for row in page]}
    if has_more:
        last = page[-1]
        result["next_cursor"] = _encode_cursor(last["created_at"], last["movement_id"])
    return result
