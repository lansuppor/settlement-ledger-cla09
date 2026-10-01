import sqlite3
from datetime import UTC, datetime

from app.store.outcome import Outcome, deserialize, serialize


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def lookup_replay(conn: sqlite3.Connection, tenant: str, operation: str,
                  target_id: str, fingerprint: str) -> Outcome | None:
    """Return the recorded first result for (tenant, operation, target, fingerprint), if any."""
    row = conn.execute(
        "SELECT result_code, result_status, result_detail, result_body "
        "FROM idempotent_requests WHERE tenant=? AND operation=? AND target_id=? AND request_fingerprint=?",
        (tenant, operation, target_id, fingerprint),
    ).fetchone()
    if row is None:
        return None
    status, detail, body = deserialize(row["result_body"])
    return Outcome(row["result_code"], status, detail, body if body is not None else {})


def store_outcome(conn: sqlite3.Connection, *, tenant: str, operation: str, target_id: str,
                  fingerprint: str, code: str, status: int, detail: str,
                  body: dict | None) -> None:
    conn.execute(
        "INSERT INTO idempotent_requests(tenant, operation, target_id, request_fingerprint, "
        "result_code, result_status, result_detail, result_body, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (tenant, operation, target_id, fingerprint, code, status, detail,
         serialize(body, status, detail), now_iso()),
    )


def record_and_finish(conn: sqlite3.Connection, *, tenant: str, operation: str, target_id: str,
                      fingerprint: str, status: int, code: str, detail: str) -> Outcome:
    """Persist a terminal (non-2xx) outcome for replay, then commit."""
    store_outcome(conn, tenant=tenant, operation=operation, target_id=target_id,
                  fingerprint=fingerprint, code=code, status=status, detail=detail, body=None)
    conn.execute("COMMIT")
    return Outcome(code, status, detail, {})
