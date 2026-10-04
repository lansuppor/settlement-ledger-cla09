import base64
import json
import sqlite3

from app.store.db import connect
from app.store.idempotency import lookup_replay, now_iso, record_and_finish, store_outcome
from app.store.outcome import Outcome, deserialize, serialize

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

# Event-stream operation labels (the public audit trail uses English tokens,
# the same convention as status/direction).
ACCEPT_EVENT = "accept"
REVERSE_EVENT = "reverse"

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 500

# Batch reversal operation labels and the distinguishable retryable-conflict
# answer returned only when write contention prevents a batch from starting.
BATCH_REVERSE_OP = "stock_movement_batch_reverse"
RETRYABLE_CONFLICT = "retryable_conflict"


class BatchRetryableConflict(Exception):
    """The write lock could not be acquired to start processing the batch.

    Raised only when the request never entered business validation, so no
    batch record exists and the batch id remains free to retry.
    """


class InvalidCursor(Exception):
    """The pagination cursor is malformed or was not issued by this service."""


class OrderSummaryNotFound(Exception):
    """The requested order has no stock movements (or does not exist here).

    Raised for the single-order summary lookup; the two cases are deliberately
    indistinguishable so the response never leaks whether the order exists.
    """


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

_EVENT_SELECT = (
    "SELECT occurred_at, operation, status, seq, direction, quantity "
    "FROM stock_movement_events"
)


def _append_event(conn: sqlite3.Connection, *, tenant: str, movement_id: str,
                  operation: str, status: str, direction: str, quantity: int,
                  occurred_at: str) -> None:
    """Append an audit row inside the caller's (already open) transaction.

    The sequence number continues per movement from 1; accept is always 1 and
    reverse — if it ever happens — is 2. A per-movement unique index on
    operation is the backstop that keeps each event to at most one row, so the
    audit trail can never disagree with how many operations took effect.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
        "FROM stock_movement_events WHERE tenant=? AND movement_id=?",
        (tenant, movement_id),
    ).fetchone()
    conn.execute(
        "INSERT INTO stock_movement_events(tenant, movement_id, seq, operation, status, "
        "direction, quantity, occurred_at) VALUES(?,?,?,?,?,?,?,?)",
        (tenant, movement_id, row["next_seq"], operation, status,
         direction, quantity, occurred_at),
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
            "INSERT INTO stock_movements(tenant, movement_id, order_id, direction, quantity, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, movement_id, order_id, direction, quantity, ACCEPTED, created_at),
        )
        _append_event(conn, tenant=tenant, movement_id=movement_id, operation=ACCEPT_EVENT,
                      status=ACCEPTED, direction=direction, quantity=quantity,
                      occurred_at=created_at)
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
        _append_event(conn, tenant=tenant, movement_id=movement_id, operation=REVERSE_EVENT,
                      status=REVERSED, direction=row["direction"], quantity=row["quantity"],
                      occurred_at=now_iso())
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


def _store_batch_outcome(conn: sqlite3.Connection, *, tenant: str, batch_id: str,
                         ids_key: str, outcome: Outcome) -> None:
    """Persist a batch's first outcome (success or recorded 404/409)."""
    conn.execute(
        "INSERT INTO stock_movement_reversal_batches(tenant, batch_id, movement_ids, "
        "result_code, result_status, result_detail, result_body, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (tenant, batch_id, ids_key, outcome.code, outcome.status, outcome.detail,
         serialize(outcome.body, outcome.status, outcome.detail), now_iso()),
    )


def _replay_batch(row: sqlite3.Row) -> Outcome:
    status, detail, body = deserialize(row["result_body"])
    return Outcome(row["result_code"], status, detail, body if body is not None else {})


def batch_reverse(tenant: str, batch_id: str, movement_ids: list[str]) -> Outcome:
    """Reverse a set of accepted movements from one mistaken registration, atomically.

    Either every listed movement flips accepted -> reversed in one transaction
    (each keeping direction/quantity/created_at and gaining one reverse event)
    or nothing changes at all. The batch is durably identified by
    (tenant, batch_id): the first legal request persists its canonical
    (sorted, deduplicated) id set together with its business result, and every
    later request with the same id set — reordered, after restart, or after the
    movements changed — replays that first result, including a first-time
    404/409. The same batch id with a different id set conflicts (409) without
    overwriting the record; a failed batch needs a fresh batch id.

    Error precedence: if any id is missing for this tenant (including an id
    that exists under another tenant — existence is never leaked), the whole
    batch answers 404; otherwise if any movement is already reversed, it
    answers 409. A single movement can actually reverse at most once, no
    matter how single reversals and batches interleave concurrently: losing
    batches fail whole with 409 and leave no partial state, events or success
    record. ``BatchRetryableConflict`` is raised only when write contention
    stops processing from starting, which never consumes the batch id.
    """
    ids_key = json.dumps(sorted(set(movement_ids)), separators=(",", ":"))

    conn = connect()
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise BatchRetryableConflict(str(error)) from error
            raise

        recorded = conn.execute(
            "SELECT result_code, result_status, result_detail, result_body, movement_ids "
            "FROM stock_movement_reversal_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if recorded is not None:
            outcome = _replay_batch(recorded)
            if recorded["movement_ids"] != ids_key:
                conn.execute("COMMIT")
                return Outcome(CONFLICT, 409,
                               "reversal batch was already submitted with a different movement set",
                               {})
            conn.execute("COMMIT")
            return outcome

        # Fetch in the canonical order; the response list is sorted by id.
        rows = conn.execute(
            f"{_SELECT} WHERE tenant=? AND movement_id IN ({','.join('?' for _ in movement_ids)}) "
            "ORDER BY movement_id ASC",
            (tenant, *movement_ids),
        ).fetchall() if movement_ids else []

        if len({r["movement_id"] for r in rows}) != len(set(movement_ids)):
            outcome = Outcome(NOT_FOUND, 404, "stock movement not found", {})
            _store_batch_outcome(conn, tenant=tenant, batch_id=batch_id,
                                 ids_key=ids_key, outcome=outcome)
            conn.execute("COMMIT")
            return outcome

        if any(r["status"] == REVERSED for r in rows):
            outcome = Outcome(CONFLICT, 409, "stock movement already reversed", {})
            _store_batch_outcome(conn, tenant=tenant, batch_id=batch_id,
                                 ids_key=ids_key, outcome=outcome)
            conn.execute("COMMIT")
            return outcome

        reversed_at = now_iso()
        results: list[dict] = []
        for row in rows:
            conn.execute(
                "UPDATE stock_movements SET status=? WHERE tenant=? AND movement_id=? AND status=?",
                (REVERSED, tenant, row["movement_id"], ACCEPTED),
            )
            _append_event(conn, tenant=tenant, movement_id=row["movement_id"],
                          operation=REVERSE_EVENT, status=REVERSED,
                          direction=row["direction"], quantity=row["quantity"],
                          occurred_at=reversed_at)
            results.append({
                "movement_id": row["movement_id"],
                "order_id": row["order_id"],
                "direction": row["direction"],
                "quantity": row["quantity"],
                "status": REVERSED,
                "created_at": row["created_at"],
            })

        body = {"batch_id": batch_id, "results": results}
        outcome = Outcome.ok(body)
        _store_batch_outcome(conn, tenant=tenant, batch_id=batch_id,
                             ids_key=ids_key, outcome=outcome)
        conn.execute("COMMIT")
        return outcome
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
    if not isinstance(order_id, str) or not order_id or set(payload) != {"order_id"}:
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


def encode_monthly_cursor(order_id: str, month: str) -> str:
    raw = json.dumps({"order_id": order_id, "month": month}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_monthly_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        order_id = payload["order_id"]
        month = payload["month"]
    except Exception as error:
        raise InvalidCursor("cursor is invalid") from error
    if (not isinstance(order_id, str) or not order_id
            or not isinstance(month, str) or not month
            or set(payload) != {"order_id", "month"}):
        raise InvalidCursor("cursor is invalid")
    return order_id, month


def monthly_summary(tenant: str, *, order_id: str | None = None, cursor: str | None = None,
                    limit: int = DEFAULT_PAGE_LIMIT) -> dict:
    """Aggregate this tenant's movements into per-(order, month) net quantities.

    The month of a movement is the UTC year-month (``YYYY-MM``) of its
    ``created_at``, fixed at accept time and never changing afterwards.
    ``net_quantity`` per (order, month) = SUM(quantity of in, accepted)
    - SUM(quantity of out, accepted) within that month; reversed movements
    count 0 regardless of their original direction, so each monthly row always
    equals the per-movement tally of that month after any accept/reverse/replay
    sequence, and the per-month nets of an order sum to its per-order
    ``summary`` net. With ``order_id`` only that order's monthly rows are
    returned, or ``OrderSummaryNotFound`` when it has no movements
    (indistinguishable from a missing/cross-tenant order). Without ``order_id``
    every (order, month) pair that has movements is listed, ordered by
    order_id ASC then month ASC — independent of insertion order and stable
    across repeated queries. Keyset pagination positions the cursor at the
    last (order_id, month) of the previous page and continues strictly after
    it, so pages never repeat or skip an entry and the union of pages in any
    order equals the full result set. Read-only: never changes any document.
    """
    clauses = ["tenant=?"]
    params: list[object] = [tenant]
    if order_id is not None:
        clauses.append("order_id=?")
        params.append(order_id)
    if cursor is not None:
        cur_order_id, cur_month = decode_monthly_cursor(cursor)
        clauses.append(
            "(order_id>? OR (order_id=? AND substr(created_at, 1, 7)>?))"
        )
        params.extend((cur_order_id, cur_order_id, cur_month))

    sql = (
        "SELECT order_id, substr(created_at, 1, 7) AS month, "
        "SUM(CASE WHEN status='accepted' AND direction='in' THEN quantity "
        "WHEN status='accepted' AND direction='out' THEN -quantity "
        "ELSE 0 END) AS net_quantity "
        f"FROM stock_movements WHERE {' AND '.join(clauses)} "
        "GROUP BY order_id, month ORDER BY order_id ASC, month ASC LIMIT ?"
    )

    conn = connect()
    try:
        rows = conn.execute(sql, (*params, limit + 1)).fetchall()
    finally:
        conn.close()

    if order_id is not None and not rows:
        raise OrderSummaryNotFound("order has no stock movement summary")

    has_more = len(rows) > limit
    page = [{"order_id": row["order_id"], "month": row["month"],
             "net_quantity": row["net_quantity"]}
            for row in rows[:limit]]
    next_cursor = (encode_monthly_cursor(page[-1]["order_id"], page[-1]["month"])
                   if has_more else None)
    return {"items": page, "next_cursor": next_cursor}


def events(tenant: str, movement_id: str) -> list[dict] | None:
    """Return a movement's audit trail ordered by (occurred_at ASC, seq ASC).

    ``None`` means the movement does not exist for this tenant (including a
    movement that belongs to another tenant), so the caller answers 404 without
    leaking existence. An existing movement that was never acted on cannot
    occur (accept always writes its event in the same transaction), but an empty
    list is returned rather than erroring. Read-only: never changes any
    document, task or order.
    """
    conn = connect()
    try:
        owner = conn.execute(
            "SELECT 1 FROM stock_movements WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()
        if owner is None:
            return None
        rows = conn.execute(
            f"{_EVENT_SELECT} WHERE tenant=? AND movement_id=? "
            "ORDER BY occurred_at ASC, seq ASC",
            (tenant, movement_id),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "occurred_at": row["occurred_at"],
            "operation": row["operation"],
            "status": row["status"],
            "seq": row["seq"],
            "direction": row["direction"],
            "quantity": row["quantity"],
        }
        for row in rows
    ]
