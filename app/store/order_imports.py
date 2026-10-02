import csv
import hashlib
import sqlite3

from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store.db import connect
from app.store.idempotency import now_iso
from app.store.outcome import Outcome

# 订单批量导入任务：提交即受理任务并立即逐行校验受理。
# 任务生命周期只有 pending（未完成，可续跑）-> completed（已完成，终态）。
# 每行在独立的立即写事务内判定：要么受理为订单，要么记录失败原因，
# 事务提交后该行才算已决，因此服务中断不会留下“半行”，也不会受理同一行两次。
PENDING = "pending"
COMPLETED = "completed"
ACCEPTED = "accepted"
FAILED = "failed"

# 可区分的错误类别（沿用其他 store 的约定）。
NOT_FOUND = "not_found"
CONFLICT = "conflict"

HEADER = ["tenant", "order_id", "amount_cents", "currency"]


class InvalidImportRequest(ValueError):
    """请求级参数不合法（400），任务不会被创建。"""


def _body(row: sqlite3.Row, errors: list[dict]) -> dict:
    return {
        "task_id": row["task_id"],
        "tenant": row["tenant"],
        "status": row["status"],
        "total_rows": row["total_rows"],
        "success_count": row["success_count"],
        "failure_count": row["failure_count"],
        "processed_count": row["processed_count"],
        "errors": errors,
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def get(tenant: str, task_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, task_id, status, total_rows, success_count, failure_count, "
            "processed_count, created_at, completed_at "
            "FROM import_tasks WHERE tenant=? AND task_id=?",
            (tenant, task_id),
        ).fetchone()
        if row is None:
            return None
        errors = _load_errors(conn, tenant, task_id)
    finally:
        conn.close()
    return _body(row, errors)


def _load_errors(conn: sqlite3.Connection, tenant: str, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT line_number, raw_line, error_reason FROM import_task_rows "
        "WHERE tenant=? AND task_id=? AND status=? ORDER BY line_number",
        (tenant, task_id, FAILED),
    ).fetchall()
    return [
        {"line_number": r["line_number"], "raw_line": r["raw_line"], "reason": r["error_reason"]}
        for r in rows
    ]


def _split_line(raw_line: str) -> list[str]:
    try:
        return [f.strip() for f in next(csv.reader([raw_line]))]
    except csv.Error:
        return []


def _parse_csv(content: str) -> list[dict]:
    """Parse submitted CSV into data-row inputs; raise InvalidImportRequest on bad shape.

    表头必须与订单示例一致（tenant,order_id,amount_cents,currency）；数据区行号从 1 起。
    行级问题（列数不符、字段非法、订单已存在、批内重复）不在此拒绝，
    留给逐行判定并记入错误清单。
    """
    content = content.removeprefix("\ufeff")
    raw_lines = content.splitlines()
    if not raw_lines:
        raise InvalidImportRequest("csv content is empty")
    header = _split_line(raw_lines[0])
    if header != HEADER:
        raise InvalidImportRequest(
            "csv header must be exactly: tenant,order_id,amount_cents,currency"
        )

    rows: list[dict] = []
    for raw_line in raw_lines[1:]:
        if not raw_line.strip():
            # 完全空白行不计入数据区、不参与编号。
            continue
        fields = _split_line(raw_line)
        padded = fields + [""] * max(0, 4 - len(fields))
        rows.append({
            "line_number": len(rows) + 1,
            "raw_line": raw_line,
            "tenant_value": padded[0],
            "order_id_value": padded[1],
            "amount_text": padded[2],
            "currency_value": padded[3],
        })
    if not rows:
        raise InvalidImportRequest("csv contains a header but no data rows")
    return rows


def _insert_order(conn: sqlite3.Connection, tenant: str, order_id: str,
                  amount_cents: int, currency: str) -> None:
    """Insert an accepted order. Indirection lets tests simulate a service
    interruption mid-import."""
    conn.execute(
        "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
        "VALUES(?,?,?,0,?,'accepted')",
        (tenant, order_id, amount_cents, currency),
    )


def accept(owner_tenant: str, task_id: str, csv_content: str) -> Outcome:
    """Submit an import task and execute row-by-row immediately.

    任务以（提交租户, 任务标识）唯一，以内容指纹区分重放与冲突：
    - 同标识同内容：重放，继续把未决行跑完后返回首次业务结果（含首次错误），
      不产生第二次受理；重启后同样识别为重放（指纹持久化在任务行上）；
    - 同标识不同内容：409，不改变已存在任务与订单数据。
    """
    rows = _parse_csv(csv_content)
    content_sha = hashlib.sha256(csv_content.encode("utf-8")).hexdigest()

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT content_sha256 FROM import_tasks WHERE tenant=? AND task_id=?",
            (owner_tenant, task_id),
        ).fetchone()
        if existing is not None:
            same = existing["content_sha256"] == content_sha
            conn.execute("COMMIT")
            if not same:
                return Outcome(CONFLICT, 409,
                               "import task already submitted with different content", {})
            # 同内容重放：若首次执行被中断，只把剩余未决行继续跑完；
            # 已决行不会被二次受理或二次记录。
            return Outcome.created(_drive(owner_tenant, task_id))

        ts = now_iso()
        conn.execute(
            "INSERT INTO import_tasks(tenant, task_id, content_sha256, total_rows, "
            "success_count, failure_count, processed_count, status, created_at) "
            "VALUES(?,?,?,?,0,0,0,?,?)",
            (owner_tenant, task_id, content_sha, len(rows), PENDING, ts),
        )
        conn.executemany(
            "INSERT INTO import_task_rows(tenant, task_id, line_number, tenant_value, "
            "order_id_value, amount_text, currency_value, raw_line, status) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (owner_tenant, task_id, r["line_number"], r["tenant_value"],
                 r["order_id_value"], r["amount_text"], r["currency_value"],
                 r["raw_line"], PENDING)
                for r in rows
            ],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    return Outcome.created(_drive(owner_tenant, task_id))


def resume(owner_tenant: str, task_id: str) -> Outcome:
    """继续一个未完成（或已完成）的导入任务：只处理尚未处理的行。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM import_tasks WHERE tenant=? AND task_id=?",
            (owner_tenant, task_id),
        ).fetchone()
        conn.execute("COMMIT")
    finally:
        conn.close()
    if existing is None:
        return Outcome(NOT_FOUND, 404, "import task not found", {})
    return Outcome.ok(_drive(owner_tenant, task_id))


def _drive(owner_tenant: str, task_id: str) -> dict:
    """Process every still-pending row, each in its own immediate transaction.

    任一非预期异常都会随当前行事务回滚后向上抛出（任务保持 pending，
    已提交的行不丢、不重），调用方可再次 resume/重放续跑。
    """
    # 行字段在任务创建后不可变；批内重复只按行号先后判定，先一次性建好视图。
    conn = connect()
    try:
        all_rows = conn.execute(
            "SELECT line_number, tenant_value, order_id_value, amount_text, currency_value "
            "FROM import_task_rows WHERE tenant=? AND task_id=? ORDER BY line_number",
            (owner_tenant, task_id),
        ).fetchall()
    finally:
        conn.close()

    first_line_by_content: dict[tuple, int] = {}
    first_line_by_key: dict[tuple, int] = {}
    for r in all_rows:
        content_key = (r["tenant_value"], r["order_id_value"],
                       r["amount_text"], r["currency_value"])
        first_line_by_content.setdefault(content_key, r["line_number"])
        first_line_by_key.setdefault((r["tenant_value"], r["order_id_value"]), r["line_number"])

    while True:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT tenant, task_id, status, total_rows, success_count, failure_count, "
                "processed_count, created_at, completed_at "
                "FROM import_tasks WHERE tenant=? AND task_id=?",
                (owner_tenant, task_id),
            ).fetchone()
            if task["status"] == COMPLETED:
                body = _body(task, _load_errors(conn, owner_tenant, task_id))
                conn.execute("COMMIT")
                return body

            row = conn.execute(
                "SELECT line_number, raw_line, tenant_value, order_id_value, amount_text, "
                "currency_value FROM import_task_rows "
                "WHERE tenant=? AND task_id=? AND status=? ORDER BY line_number LIMIT 1",
                (owner_tenant, task_id, PENDING),
            ).fetchone()
            if row is None:
                body = _finalize(conn, task)
                conn.execute("COMMIT")
                return body
            _decide_row(conn, task, row, first_line_by_content, first_line_by_key)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _finalize(conn: sqlite3.Connection, task: sqlite3.Row) -> dict:
    """All rows decided: flip the task to completed when counts agree."""
    if task["processed_count"] == task["total_rows"]:
        conn.execute(
            "UPDATE import_tasks SET status=?, completed_at=? "
            "WHERE tenant=? AND task_id=? AND status=?",
            (COMPLETED, now_iso(), task["tenant"], task["task_id"], PENDING),
        )
        task = conn.execute(
            "SELECT tenant, task_id, status, total_rows, success_count, failure_count, "
            "processed_count, created_at, completed_at "
            "FROM import_tasks WHERE tenant=? AND task_id=?",
            (task["tenant"], task["task_id"]),
        ).fetchone()
    return _body(task, _load_errors(conn, task["tenant"], task["task_id"]))


def _decide_row(conn: sqlite3.Connection, task: sqlite3.Row, row: sqlite3.Row,
                first_line_by_content: dict[tuple, int],
                first_line_by_key: dict[tuple, int]) -> None:
    """Decide one row atomically: accept one order or record one failure reason."""
    line = row["line_number"]
    tenant_value = row["tenant_value"]
    order_id_value = row["order_id_value"]
    amount_text = row["amount_text"]
    currency_value = row["currency_value"]

    content_key = (tenant_value, order_id_value, amount_text, currency_value)
    order_key = (tenant_value, order_id_value)

    reason = ""
    # 批内重复优先判定：即使先到行自身因其他原因不合格，重复行也只记重复错误。
    if first_line_by_content[content_key] < line:
        reason = "duplicate row in import: identical line already present"
    elif first_line_by_key[order_key] < line:
        reason = "duplicate order in import: order_id already used by an earlier line"
    elif len(_split_line(row["raw_line"])) != 4:
        reason = "row must have exactly 4 columns: tenant,order_id,amount_cents,currency"
    elif not tenant_value:
        reason = "tenant must be a non-empty string"
    elif not order_id_value:
        reason = "order_id must be a non-empty string"
    elif not _is_positive_int(amount_text):
        reason = "amount_cents must be a positive integer"
    elif currency_value not in ALLOWED_CURRENCIES:
        reason = f"unsupported currency: {currency_value or '(empty)'}"
    elif conn.execute(
        "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
        (tenant_value, order_id_value),
    ).fetchone() is not None:
        reason = "order already exists for tenant"

    if not reason:
        try:
            _insert_order(conn, tenant_value, order_id_value, int(amount_text), currency_value)
        except sqlite3.IntegrityError:
            # 唯一约束兜底：与批外并发受理撞单时按失败行记录，不影响其他行。
            reason = "order already exists for tenant"

    flipped = conn.execute(
        "UPDATE import_task_rows SET status=?, error_reason=? "
        "WHERE tenant=? AND task_id=? AND line_number=? AND status=?",
        (FAILED if reason else ACCEPTED, reason,
         task["tenant"], task["task_id"], line, PENDING),
    )
    # 行可能已被并发的续跑决定：rowcount==0 时不动计数，交下一轮跳过。
    if flipped.rowcount == 0:
        return

    delta_col = "failure_count" if reason else "success_count"
    conn.execute(
        f"UPDATE import_tasks SET {delta_col}={delta_col}+1, "
        "processed_count=processed_count+1 WHERE tenant=? AND task_id=?",
        (task["tenant"], task["task_id"]),
    )


def _is_positive_int(text: str) -> bool:
    if not text or not text.isascii() or not text.isdigit():
        return False
    return int(text) > 0
