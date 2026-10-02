import csv
import hashlib
import threading

from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store.db import connect
from app.store.idempotency import now_iso
from app.store.outcome import Outcome

# 订单批量导入任务：受理后逐行校验并受理订单。
#
# 任务生命周期：in_progress（尚有行未处理；服务中断后仍停留此态）
#   -> completed（全部行判定完毕，终态）。
#
# 每行在各自的 BEGIN IMMEDIATE 事务内判定：合格行同事务写入订单并置行
# success；不合格行只置行 failure 并记录原因；事务中断回滚则行仍为
# pending，续跑时重新判定，绝不留下“半行”，也不会把同一行受理两次。
IN_PROGRESS = "in_progress"
COMPLETED = "completed"

ROW_PENDING = "pending"
ROW_SUCCESS = "success"
ROW_FAILURE = "failure"

# 可区分错误类别（与其他 store 约定一致）。
NOT_FOUND = "not_found"
# 同一任务标识、不同内容的先后（非并发）重复受理。
CONFLICT = "task_already_accepted"
# 同一任务标识、不同内容与正在执行的首跑并发相撞。
CONCURRENT = "concurrent_conflict"
INVALID_CSV = "invalid_csv"

EXPECTED_HEADER = ["tenant", "order_id", "amount_cents", "currency"]

# 单进程并发模型：
#   - 每个任务一把执行锁，串行化真正的逐行处理；
#   - _RUNNING 记录“此刻持有执行锁、正在跑”的任务，用于区分先后重复受理
#     与并发冲突。
# 同内容请求（含并发）阻塞等待首跑结束后返回与首次完全相同的结果，绝不
# 二次受理；不同内容请求不阻塞：首跑在执行即并发冲突，否则视为先后重复
# 受理。服务重启后锁与集合清空，任务以 in_progress 持久化落库，仍可续跑。
_REGISTRY_LOCK = threading.Lock()
_RUN_LOCKS: dict[str, threading.Lock] = {}
_RUNNING: set[str] = set()


def _run_lock(task_id: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _RUN_LOCKS.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _RUN_LOCKS[task_id] = lock
        return lock


def _execute(task_id: str) -> None:
    """持有执行锁跑完剩余行：与其它执行互斥，崩溃时清状态并向上抛出。"""
    lock = _run_lock(task_id)
    lock.acquire()
    try:
        with _REGISTRY_LOCK:
            _RUNNING.add(task_id)
        _run(task_id)
    finally:
        with _REGISTRY_LOCK:
            _RUNNING.discard(task_id)
        lock.release()


class _Plan:
    """一行在“不依赖数据库”层面的预判定结果（纯 CSV 内容的函数）。

    重复关系完全由内容决定并预先算好，因此一次性导入与任意断点续跑对
    同一行给出完全相同的判定；reason 非 None 表示该行确定性失败，
    为 None 表示合格候选，是否受理还需在事务内查订单是否已存在。
    """

    __slots__ = (
        "amount_cents",
        "currency",
        "line_no",
        "order_id",
        "raw",
        "reason",
        "tenant",
    )

    def __init__(self, line_no: int, raw: str, tenant: str, order_id: str,
                 amount_cents: int | None, currency: str, reason: str | None):
        self.line_no = line_no
        self.raw = raw
        self.tenant = tenant
        self.order_id = order_id
        self.amount_cents = amount_cents
        self.currency = currency
        self.reason = reason


def _parse_csv(content: str) -> list[_Plan]:
    """解析并预判定整份 CSV；任何整单级不合法情形抛 ValueError。

    以物理行一一对应数据行（行号从 1 起计）；逐行经 csv 解析以支持引号
    包裹的逗号，但不支持跨行字段。splitlines 已去除行终止符并吞掉末尾
    换行，CRLF 与结尾空行都能得到确定结果。
    """
    if not isinstance(content, str) or not content.strip():
        raise ValueError("csv content is required")

    lines = content.splitlines()
    header = next(csv.reader([lines[0]]))
    if [c.strip() for c in header] != EXPECTED_HEADER:
        raise ValueError(f"csv header must be: {','.join(EXPECTED_HEADER)}")
    if len(lines) < 2:
        raise ValueError("csv must contain at least one data row")

    data_lines = lines[1:]
    rows = [next(csv.reader([line]), []) for line in data_lines]

    # 第一遍：在结构合格、身份字段（租户、订单标识）齐备的候选行里，
    # 记录完全相同行与同订单标识行各自“先到”的行号。
    first_exact: dict[tuple, int] = {}
    first_order: dict[tuple, int] = {}
    for offset, cols in enumerate(rows, start=1):
        if len(cols) != 4:
            continue
        tenant = cols[0].strip()
        order_id = cols[1].strip()
        if not tenant or not order_id:
            continue
        exact = (tenant, order_id, cols[2].strip(), cols[3].strip())
        first_exact.setdefault(exact, offset)
        first_order.setdefault((tenant, order_id), offset)

    plans: list[_Plan] = []
    for offset, cols in enumerate(rows, start=1):
        raw = data_lines[offset - 1]
        if len(cols) != 4:
            plans.append(_Plan(offset, raw, "", "", None, "",
                               "row must contain exactly 4 columns: tenant,order_id,amount_cents,currency"))
            continue
        tenant = cols[0].strip()
        order_id = cols[1].strip()
        amount_text = cols[2].strip()
        currency = cols[3].strip()
        if not tenant:
            plans.append(_Plan(offset, raw, tenant, order_id, None, currency,
                               "tenant must be a non-empty string"))
            continue
        if not order_id:
            plans.append(_Plan(offset, raw, tenant, order_id, None, currency,
                               "order_id must be a non-empty string"))
            continue

        # 重复判定先于本行金额/币种/库内查重：先到的一行只受理一次，
        # 后到行（哪怕自身另有瑕疵）一律记为重复错误。
        if first_exact[(tenant, order_id, amount_text, currency)] < offset:
            reason: str | None = "duplicate row in import"
        elif first_order[(tenant, order_id)] < offset:
            reason = "duplicate order_id in import"
        else:
            reason = None

        amount_cents = int(amount_text) if _is_uint(amount_text) else None
        if reason is None and (amount_cents is None or amount_cents <= 0):
            reason = "amount must be a positive integer"
        if reason is None and currency not in ALLOWED_CURRENCIES:
            reason = "unsupported currency"

        plans.append(_Plan(offset, raw, tenant, order_id, amount_cents,
                           currency, reason))
    return plans


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _is_uint(text: str) -> bool:
    """正整数（不含符号、小数点、空白与 Unicode 数字）的严格判定。"""
    return bool(text) and all("0" <= c <= "9" for c in text)


def _task_body(row, counts: tuple[int, int, int], errors: list[dict]) -> dict:
    success, failure, processed = counts
    return {
        "task_id": row["task_id"],
        "status": row["status"],
        "total_rows": row["total_rows"],
        "success_count": success,
        "failure_count": failure,
        "processed_rows": processed,
        "errors": errors,
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _load_view(conn, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT task_id, total_rows, status, created_at, completed_at "
        "FROM order_import_tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    error_rows = conn.execute(
        "SELECT line_no, raw, error_reason FROM order_import_task_rows "
        "WHERE task_id=? AND status=? ORDER BY line_no",
        (task_id, ROW_FAILURE),
    ).fetchall()
    errors = [{"line_no": r["line_no"], "raw": r["raw"],
               "reason": r["error_reason"]} for r in error_rows]
    totals = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status=? THEN 1 ELSE 0 END) AS success_count, "
        "SUM(CASE WHEN status=? THEN 1 ELSE 0 END) AS failure_count, "
        "SUM(CASE WHEN status!=? THEN 1 ELSE 0 END) AS processed_count "
        "FROM order_import_task_rows WHERE task_id=?",
        (ROW_SUCCESS, ROW_FAILURE, ROW_PENDING, task_id),
    ).fetchone()
    counts = (totals["success_count"] or 0, totals["failure_count"] or 0,
              totals["processed_count"] or 0)
    return {"body": _task_body(row, counts, errors)}


def get(task_id: str) -> dict | None:
    conn = connect()
    try:
        view = _load_view(conn, task_id)
    finally:
        conn.close()
    return None if view is None else view["body"]


def _process_row(conn, task_id: str, plan: _Plan) -> None:
    """在已持有 IMMEDIATE 写事务的连接上判定并落库一行。

    行在任务受理时已预置为 pending，这里把它原子地翻转为 failure 或
    success；成功行的订单写入与行翻转同事务提交。
    """
    if plan.reason is not None:
        conn.execute(
            "UPDATE order_import_task_rows SET status=?, error_reason=? "
            "WHERE task_id=? AND line_no=? AND status=?",
            (ROW_FAILURE, plan.reason, task_id, plan.line_no, ROW_PENDING),
        )
        return
    # 订单写入与行成功判定同事务提交：中断即整行回滚为 pending，不留半行。
    # INSERT OR IGNORE + 命中数判定把“查存在”与“写入”合成一次原子操作，
    # 与直接受理订单或其他导入任务并发撞同一订单标识时不会报内部错误，
    # 而是落到下面的“已存在”失败分支。
    cursor = conn.execute(
        "INSERT OR IGNORE INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) "
        "VALUES(?,?,?,0,?,'accepted')",
        (plan.tenant, plan.order_id, plan.amount_cents, plan.currency),
    )
    if cursor.rowcount == 0:
        conn.execute(
            "UPDATE order_import_task_rows SET status=?, error_reason=? "
            "WHERE task_id=? AND line_no=? AND status=?",
            (ROW_FAILURE, "order already accepted", task_id, plan.line_no, ROW_PENDING),
        )
        return
    conn.execute(
        "UPDATE order_import_task_rows SET status=?, error_reason='' "
        "WHERE task_id=? AND line_no=? AND status=?",
        (ROW_SUCCESS, task_id, plan.line_no, ROW_PENDING),
    )


def _run(task_id: str) -> None:
    """逐行处理所有尚未判定的行，全部完成后把任务置为 completed。

    每行各自一个 IMMEDIATE 事务：已判定行直接跳过（不重复受理、不重复
    记失败），订单写入与行判定同事务提交。重复关系是 CSV 内容的预算
    结果，故任意中断后续跑与一次性成功导入的最终结果完全一致。
    """
    conn = connect()
    try:
        csv_content = conn.execute(
            "SELECT csv_content FROM order_import_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()["csv_content"]
    finally:
        conn.close()
    plans = _parse_csv(csv_content)

    for plan in plans:
        row_conn = connect()
        try:
            row_conn.execute("BEGIN IMMEDIATE")
            state = row_conn.execute(
                "SELECT status FROM order_import_task_rows WHERE task_id=? AND line_no=?",
                (task_id, plan.line_no),
            ).fetchone()
            if state is not None and state["status"] != ROW_PENDING:
                row_conn.execute("COMMIT")
                continue
            _process_row(row_conn, task_id, plan)
            row_conn.execute("COMMIT")
        except Exception:
            row_conn.execute("ROLLBACK")
            raise
        finally:
            row_conn.close()

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        remaining = conn.execute(
            "SELECT COUNT(1) AS c FROM order_import_task_rows WHERE task_id=? AND status=?",
            (task_id, ROW_PENDING),
        ).fetchone()["c"]
        if remaining == 0:
            conn.execute(
                "UPDATE order_import_tasks SET status=?, completed_at=? "
                "WHERE task_id=? AND status!=?",
                (COMPLETED, now_iso(), task_id, COMPLETED),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()


def submit(task_id: str, content: str) -> Outcome:
    """受理导入任务并立即逐行执行；同一任务标识重放首次业务结果。

    - 新任务：落库任务与全部 pending 行，认领执行后同步跑到 completed，
      返回 201 与最终任务视图。
    - 同标识同内容（含并发）：阻塞等在执行的首跑结束（首跑曾中断则续跑
      剩余行），返回与首次完全相同的任务视图（含首次错误清单），绝不二次
      受理；任务已完成则直接返回首次结果。
    - 同标识不同内容：不阻塞、不改动已存在任务与订单；首跑正在执行返回
      并发冲突（concurrent_conflict,409），否则返回重复受理
      （task_already_accepted,409），二者 detail 可区分。

    “查存在-建任务-认领执行”在同一把注册锁内完成，新建与认领之间无空窗，
    并发不同内容提交稳定地只有一个生效。任务落库即视为已受理，执行阶段
    即便抛出内部错误，重放仍续跑而非重建。
    """
    try:
        plans = _parse_csv(content)
    except ValueError as error:
        return Outcome(INVALID_CSV, 400, str(error), {})

    digest = _content_hash(content)
    lock = _run_lock(task_id)

    # 注册锁只覆盖“判定 + 建任务 + 认领”，不覆盖耗时的逐行执行。
    with _REGISTRY_LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT content_hash, status FROM order_import_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()

            if existing is not None and existing["content_hash"] != digest:
                running = task_id in _RUNNING
                conn.execute("COMMIT")
                if running:
                    return Outcome(CONCURRENT, 409,
                                   "a concurrent submission for the same task id with different "
                                   "content is in progress", {})
                return Outcome(CONFLICT, 409,
                               "import task already accepted with different content", {})

            if existing is not None:
                already_done = existing["status"] == COMPLETED
                is_creator = False
                conn.execute("COMMIT")
            else:
                conn.execute(
                    "INSERT INTO order_import_tasks(task_id, csv_content, content_hash, total_rows, "
                    "status, created_at, completed_at) VALUES(?,?,?,?,?,?,NULL)",
                    (task_id, content, digest, len(plans), IN_PROGRESS, now_iso()),
                )
                conn.executemany(
                    "INSERT INTO order_import_task_rows(task_id, line_no, raw, status, error_reason) "
                    "VALUES(?,?,?,?,'')",
                    [(task_id, p.line_no, p.raw, ROW_PENDING) for p in plans],
                )
                conn.execute("COMMIT")
                already_done = False
                is_creator = True
                # 在仍持有注册锁时认领执行（占位 + 持执行锁）一气呵成，
                # 注册锁释放后任何并发提交都能稳定看到在跑状态。
                _RUNNING.add(task_id)
                lock.acquire()
        finally:
            conn.close()

    if is_creator:
        try:
            _run(task_id)
        finally:
            with _REGISTRY_LOCK:
                _RUNNING.discard(task_id)
            lock.release()
        return Outcome.created(get(task_id))

    # 同内容重放：已完成直接返回首次结果；未完成则（断点）续跑/等待首跑。
    if not already_done:
        _execute(task_id)
    return Outcome.created(get(task_id))


def resume(task_id: str) -> Outcome:
    """继续同一任务：只处理尚未判定的行；已完成任务为幂等成功。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM order_import_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return Outcome(NOT_FOUND, 404, "import task not found", {})
    _execute(task_id)
    return Outcome.ok(get(task_id))
