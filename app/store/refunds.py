import json
import sqlite3
from datetime import UTC, datetime

from app.store.db import connect

OP_ACCEPT = "refund.accept"
OP_REVERSE = "refund.reverse"

STATUS_PENDING = "pending"
STATUS_EFFECTIVE = "effective"
STATUS_REVERSED = "reversed"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_EFFECTIVE)


class ApiError(Exception):
    """业务错误：携带 HTTP 状态码与可区分的错误说明。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Replayed:
    """幂等重放命中：携带与首次完全相同的状态码与响应体。"""

    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self.body = body


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _shape(row: sqlite3.Row) -> dict:
    return {
        "refund_id": row["refund_id"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "reason": row["reason"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _fail(conn: sqlite3.Connection, tenant: str, operation: str, target_id: str,
          fingerprint: str, status_code: int, detail: str, created_at: str) -> None:
    """记录首次错误结果后抛出；错误结果同样可被同指纹重放。"""
    body = {"detail": detail}
    conn.execute(
        "INSERT INTO idempotent_requests(tenant, operation, target_id, request_fingerprint,"
        " response_code, response_body, created_at) VALUES(?,?,?,?,?,?,?)",
        (tenant, operation, target_id, fingerprint, status_code,
         json.dumps(body, ensure_ascii=False), created_at),
    )
    conn.execute("COMMIT")
    raise ApiError(status_code, detail)


def _find_replay(conn: sqlite3.Connection, tenant: str, operation: str,
                 target_id: str, fingerprint: str) -> Replayed | None:
    row = conn.execute(
        "SELECT response_code, response_body FROM idempotent_requests"
        " WHERE tenant=? AND operation=? AND target_id=? AND request_fingerprint=?",
        (tenant, operation, target_id, fingerprint),
    ).fetchone()
    if row is None:
        return None
    return Replayed(row["response_code"], json.loads(row["response_body"]))


def get(tenant: str, refund_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT refund_id, order_id, amount_cents, reason, status, created_at"
            " FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else _shape(row)


def accept(tenant: str, refund_id: str, order_id: str, amount_cents: int,
           reason: str, fingerprint: str) -> dict | Replayed:
    created_at = _now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replayed = _find_replay(conn, tenant, OP_ACCEPT, refund_id, fingerprint)
        if replayed is not None:
            conn.execute("ROLLBACK")
            return replayed

        if conn.execute(
            "SELECT 1 FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone() is not None:
            # 退款单已存在：不同指纹的重复受理一律拒绝，不改变已存在数据。
            _fail(conn, tenant, OP_ACCEPT, refund_id, fingerprint,
                  409, "refund already accepted", created_at)

        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 不存在 / 跨租户订单统一按“不存在”处理，不泄漏对象是否存在。
            _fail(conn, tenant, OP_ACCEPT, refund_id, fingerprint,
                  404, "order not found", created_at)

        if amount_cents > order["amount_cents"]:
            _fail(conn, tenant, OP_ACCEPT, refund_id, fingerprint,
                  400, "refund amount exceeds order amount", created_at)

        held = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS held FROM refunds"
            " WHERE tenant=? AND order_id=? AND status IN (?,?)",
            (tenant, order_id, *ACTIVE_STATUSES),
        ).fetchone()["held"]
        refundable = order["paid_cents"] - held
        if amount_cents > refundable:
            _fail(conn, tenant, OP_ACCEPT, refund_id, fingerprint,
                  409, "refund exceeds refundable amount", created_at)

        try:
            conn.execute(
                "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, reason, status, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (tenant, refund_id, order_id, amount_cents, reason, STATUS_PENDING, created_at),
            )
        except sqlite3.IntegrityError:
            # 并发下另一事务已受理同标识退款：拒绝，不改变已存在数据。
            conn.execute("ROLLBACK")
            raise ApiError(409, "refund already accepted")

        body = {
            "refund_id": refund_id,
            "order_id": order_id,
            "amount_cents": amount_cents,
            "reason": reason,
            "status": STATUS_PENDING,
            "created_at": created_at,
        }
        conn.execute(
            "INSERT INTO idempotent_requests(tenant, operation, target_id, request_fingerprint,"
            " response_code, response_body, created_at) VALUES(?,?,?,?,?,?,?)",
            (tenant, OP_ACCEPT, refund_id, fingerprint, 201,
             json.dumps(body, ensure_ascii=False), created_at),
        )
        # 占用与受理记录同一事务提交，失败不留部分占用。
        conn.execute("COMMIT")
        return body
    finally:
        conn.close()


def reverse(tenant: str, refund_id: str, fingerprint: str) -> dict | Replayed:
    created_at = _now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        replayed = _find_replay(conn, tenant, OP_REVERSE, refund_id, fingerprint)
        if replayed is not None:
            conn.execute("ROLLBACK")
            return replayed

        row = conn.execute(
            "SELECT refund_id, order_id, amount_cents, reason, status, created_at"
            " FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if row is None:
            # 不存在 / 跨租户统一 404，不泄漏对象是否存在。
            _fail(conn, tenant, OP_REVERSE, refund_id, fingerprint,
                  404, "refund not found", created_at)

        if row["status"] == STATUS_REVERSED:
            # 已冲正退款单重复冲正：冲突且不改变状态。
            _fail(conn, tenant, OP_REVERSE, refund_id, fingerprint,
                  409, "refund already reversed", created_at)

        conn.execute(
            "UPDATE refunds SET status=? WHERE tenant=? AND refund_id=? AND status IN (?,?)",
            (STATUS_REVERSED, tenant, refund_id, *ACTIVE_STATUSES),
        )
        body = {**_shape(row), "status": STATUS_REVERSED}
        conn.execute(
            "INSERT INTO idempotent_requests(tenant, operation, target_id, request_fingerprint,"
            " response_code, response_body, created_at) VALUES(?,?,?,?,?,?,?)",
            (tenant, OP_REVERSE, refund_id, fingerprint, 200,
             json.dumps(body, ensure_ascii=False), created_at),
        )
        # 状态翻转（额度释放）与冲正记录同一事务原子提交。
        conn.execute("COMMIT")
        return body
    finally:
        conn.close()
