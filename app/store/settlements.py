import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.store.db import connect

# Settlement lifecycle: pending -> effective (terminal).
# A settlement does not itself occupy refundable quota; advancing it flips
# every referenced pending refund to effective, which is what occupies quota.
PENDING = "pending"
EFFECTIVE = "effective"

# Refund lifecycle is mirrored here so advance can validate references
# without importing the refunds store. The R_ prefix keeps these status
# values distinct from the REFUND_* error codes below.
R_PENDING = "pending"
R_EFFECTIVE = "effective"
R_REVERSED = "reversed"

# Distinguishable error codes for the HTTP layer (carried in detail.code).
NOT_FOUND = "not_found"
INVALID = "invalid_request"
AMOUNT_MISMATCH = "amount_mismatch"
ALREADY_ACCEPTED = "settlement_already_accepted"
ALREADY_EFFECTIVE = "settlement_already_effective"
REFUND_ALREADY_SETTLED = "refund_already_settled"
REFUND_REVERSED = "refund_reversed"
REFUND_ORDER_MISMATCH = "refund_order_mismatch"

OP_ACCEPT = "settle_accept"
OP_ADVANCE = "settle_advance"


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


def _serialize(body: dict | None, status: int, detail: str = "") -> str:
    return json.dumps({"status": status, "detail": detail, "body": body}, ensure_ascii=False)


def _deserialize(raw: str) -> tuple[int, str, dict | None]:
    payload = json.loads(raw)
    return payload["status"], payload["detail"], payload["body"]


def _row_to_body(row: sqlite3.Row, refund_ids: list[str]) -> dict:
    return {
        "settlement_id": row["settlement_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "refund_ids": refund_ids,
        "reason": row["reason"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


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
        refs = conn.execute(
            "SELECT refund_id FROM settlement_refunds WHERE tenant=? AND settlement_id=? ORDER BY position",
            (tenant, settlement_id),
        ).fetchall()
    finally:
        conn.close()
    return _row_to_body(row, [r["refund_id"] for r in refs])


def accept(tenant: str, settlement_id: str, order_id: str, amount_cents: int,
           refund_ids: list[str], reason: str, fingerprint: str) -> Outcome:
    """Accept a settlement in pending state, idempotently.

    Acceptance is deliberately thin: the referenced refunds and the amount
    total are validated at advance time, so accepting never fails on a
    refund that is still in flight. The order must exist in the same tenant;
    a missing or cross-tenant order reads as "not found" and never reveals
    whether the object exists.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = _lookup_replay(conn, tenant, OP_ACCEPT, settlement_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return _record_and_finish(
                conn, tenant, OP_ACCEPT, settlement_id, fingerprint,
                404, NOT_FOUND, "settlement target not found",
            )

        existing = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if existing is not None:
            return _record_and_finish(
                conn, tenant, OP_ACCEPT, settlement_id, fingerprint,
                409, ALREADY_ACCEPTED, "settlement already accepted",
            )

        if not refund_ids:
            return _record_and_finish(
                conn, tenant, OP_ACCEPT, settlement_id, fingerprint,
                400, INVALID, "settlement must reference at least one refund",
            )
        if len(set(refund_ids)) != len(refund_ids):
            return _record_and_finish(
                conn, tenant, OP_ACCEPT, settlement_id, fingerprint,
                400, INVALID, "settlement references the same refund more than once",
            )

        created_at = _now()
        conn.execute(
            "INSERT INTO settlements(tenant, settlement_id, order_id, amount_cents, reason, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (tenant, settlement_id, order_id, amount_cents, reason, PENDING, created_at, created_at),
        )
        conn.executemany(
            "INSERT INTO settlement_refunds(tenant, settlement_id, refund_id, position) VALUES(?,?,?,?)",
            [(tenant, settlement_id, refund_id, position)
             for position, refund_id in enumerate(refund_ids)],
        )
        body = {
            "settlement_id": settlement_id,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "refund_ids": list(refund_ids),
            "reason": reason,
            "status": PENDING,
            "created_at": created_at,
            "updated_at": created_at,
        }
        _store_outcome(conn, tenant, OP_ACCEPT, settlement_id, fingerprint, "ok", 201, "", _serialize(body, 201))
        conn.execute("COMMIT")
        return Outcome.created(body)
    finally:
        conn.close()


def advance(tenant: str, settlement_id: str, fingerprint: str) -> Outcome:
    """Atomically advance a settlement and every refund it references.

    Either all referenced pending refunds become effective together with the
    settlement, or nothing changes and a distinguishable failure is returned.
    Replays (same tenant/operation/settlement/fingerprint) return the original
    result, including an original error, and never flip state a second time.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replay = _lookup_replay(conn, tenant, OP_ADVANCE, settlement_id, fingerprint)
        if replay is not None:
            conn.execute("COMMIT")
            return replay

        row = conn.execute(
            "SELECT order_id, amount_cents, reason, status, created_at FROM settlements "
            "WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if row is None:
            return _record_and_finish(
                conn, tenant, OP_ADVANCE, settlement_id, fingerprint,
                404, NOT_FOUND, "settlement not found",
            )

        if row["status"] == EFFECTIVE:
            return _record_and_finish(
                conn, tenant, OP_ADVANCE, settlement_id, fingerprint,
                409, ALREADY_EFFECTIVE, "settlement already effective",
            )

        refs = conn.execute(
            "SELECT refund_id FROM settlement_refunds WHERE tenant=? AND settlement_id=? ORDER BY position",
            (tenant, settlement_id),
        ).fetchall()

        total = 0
        for ref in refs:
            refund_id = ref["refund_id"]
            refund = conn.execute(
                "SELECT order_id, amount_cents, status FROM refunds WHERE tenant=? AND refund_id=?",
                (tenant, refund_id),
            ).fetchone()
            # A cross-tenant reference cannot be seen, so it reads the same as
            # a refund that does not exist.
            if refund is None:
                return _fail(conn, tenant, settlement_id, fingerprint,
                             404, NOT_FOUND, f"referenced refund not found: {refund_id}")

            if refund["order_id"] != row["order_id"]:
                return _fail(conn, tenant, settlement_id, fingerprint,
                             409, REFUND_ORDER_MISMATCH,
                             f"referenced refund belongs to another order: {refund_id}")

            if refund["status"] == R_REVERSED:
                return _fail(conn, tenant, settlement_id, fingerprint,
                             409, REFUND_REVERSED, f"referenced refund reversed: {refund_id}")

            # A pending settlement may share a pending refund with other pending
            # settlements; BEGIN IMMEDIATE serializes advances, so the first one
            # to succeed flips the refund to effective and every later advance
            # sees it here and loses with a reference conflict. Since this
            # settlement itself is still pending, an effective referenced
            # refund can only have been written off by another settlement.
            if refund["status"] == R_EFFECTIVE:
                return _fail(conn, tenant, settlement_id, fingerprint,
                             409, REFUND_ALREADY_SETTLED,
                             f"referenced refund already settled: {refund_id}")

            total += refund["amount_cents"]

        if total != row["amount_cents"]:
            return _fail(
                conn, tenant, settlement_id, fingerprint,
                400, AMOUNT_MISMATCH,
                f"settlement amount {row['amount_cents']} does not match referenced refunds total {total}",
            )

        # All checks passed inside this transaction: flip everything together.
        advanced_at = _now()
        for ref in refs:
            conn.execute(
                "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=? AND status=?",
                (R_EFFECTIVE, tenant, ref["refund_id"], R_PENDING),
            )
        conn.execute(
            "UPDATE settlements SET status=?, updated_at=? WHERE tenant=? AND settlement_id=?",
            (EFFECTIVE, advanced_at, tenant, settlement_id),
        )
        body = {
            "settlement_id": settlement_id,
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "refund_ids": [ref["refund_id"] for ref in refs],
            "reason": row["reason"],
            "status": EFFECTIVE,
            "created_at": row["created_at"],
            "updated_at": advanced_at,
        }
        _store_outcome(conn, tenant, OP_ADVANCE, settlement_id, fingerprint, "ok", 200, "", _serialize(body, 200))
        conn.execute("COMMIT")
        return Outcome.ok(body)
    finally:
        conn.close()


def _fail(conn: sqlite3.Connection, tenant: str, settlement_id: str, fingerprint: str,
          status: int, code: str, detail: str) -> Outcome:
    """Persist a failed advance for identical replay; state is left untouched."""
    return _record_and_finish(conn, tenant, OP_ADVANCE, settlement_id, fingerprint,
                              status, code, detail)


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
    _store_outcome(conn, tenant, operation, target_id, fingerprint,
                   code, status, detail, _serialize(None, status, detail))
    conn.execute("COMMIT")
    return Outcome(code, status, detail, {})
