import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.store.db import connect

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


@dataclass
class Outcome:
    code: str
    status: int
    detail: str
    body: dict

    @classmethod
    def ok(cls, body: dict) -> "Outcome":
        return cls("ok", 200, "", body)

    @classmethod
    def created(cls, body: dict) -> "Outcome":
        return cls("ok", 201, "", body)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _serialize(refund: dict | None, status: int) -> str:
    return json.dumps({"status": status, "detail": "", "body": refund}, ensure_ascii=False)


def _deserialize(raw: str) -> tuple[int, str, dict | None]:
    payload = json.loads(raw)
    return payload["status"], payload["detail"], payload["body"]


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

        replay = _lookup_replay(conn, tenant, "accept", refund_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return _record_and_finish(
                conn, tenant, "accept", refund_id, fingerprint,
                404, NOT_FOUND, "refund target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            return _record_and_finish(
                conn, tenant, "accept", refund_id, fingerprint,
                409, CONFLICT, "refund already accepted",
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
            return _record_and_finish(
                conn, tenant, "accept", refund_id, fingerprint,
                400, "invalid_amount", "refund amount must be a positive integer not exceeding order amount",
            )
        if amount_cents > refundable:
            return _record_and_finish(
                conn, tenant, "accept", refund_id, fingerprint,
                409, QUOTA, "refund exceeds refundable amount",
            )

        created_at = _now()
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
        _store_outcome(conn, tenant, "accept", refund_id, fingerprint, "ok", 201, "", _serialize(body, 201))
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def reverse(tenant: str, refund_id: str, fingerprint: str) -> Outcome:
    """Reverse a refund idempotently, releasing its occupied quota atomically."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = _lookup_replay(conn, tenant, "reverse", refund_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            "SELECT order_id, amount_cents, reason, status, created_at FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            return _record_and_finish(
                conn, tenant, "reverse", refund_id, fingerprint,
                404, NOT_FOUND, "refund not found",
            )

        if row["status"] == REVERSED:
            return _record_and_finish(
                conn, tenant, "reverse", refund_id, fingerprint,
                409, CONFLICT, "refund already reversed",
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
        _store_outcome(conn, tenant, "reverse", refund_id, fingerprint, "ok", 200, "", _serialize(body, 200))
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()


def _lookup_replay(conn: sqlite3.Connection, tenant: str, operation: str,
                   target_id: str, fingerprint: str) -> Outcome | None:
    row = conn.execute(
        "SELECT result_code, result_status, result_detail, result_body "
        "FROM idempotent_requests WHERE tenant=? AND operation=? AND target_id=? AND request_fingerprint=?",
        (tenant, operation, target_id, fingerprint),
    ).fetchone()
    if row is None:
        return None
    status, detail, body = _deserialize(row["result_body"])
    return Outcome(row["result_code"], status, detail, body if body is not None else {})


def _store_outcome(conn: sqlite3.Connection, tenant: str, operation: str, target_id: str,
                   fingerprint: str, code: str, status: int, detail: str, body: str) -> None:
    conn.execute(
        "INSERT INTO idempotent_requests(tenant, operation, target_id, request_fingerprint, "
        "result_code, result_status, result_detail, result_body, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (tenant, operation, target_id, fingerprint, code, status, detail, body, _now()),
    )


def _record_and_finish(conn: sqlite3.Connection, tenant: str, operation: str, target_id: str,
                       fingerprint: str, status: int, code: str, detail: str) -> Outcome:
    """Persist a terminal (non-2xx) outcome for replay, then commit."""
    _store_outcome(conn, tenant, operation, target_id, fingerprint,
                   code, status, detail, json.dumps(
                       {"status": status, "detail": detail, "body": None}, ensure_ascii=False))
    conn.execute("COMMIT")
    return Outcome(code, status, detail, {})
