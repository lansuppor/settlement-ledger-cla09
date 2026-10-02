import os
import tempfile
import threading

import pytest

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_imports.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store import order_imports
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

HEADER = "tenant,order_id,amount_cents,currency\n"


def submit(tenant: str, task_id: str, csv_content: str):
    return client.post(
        "/order-imports",
        headers={"X-Tenant": tenant},
        json={"task_id": task_id, "csv_content": csv_content},
    )


def test_import_valid_and_invalid_rows() -> None:
    csv = HEADER + (
        "t1,imp-1,1000,CNY\n"
        "t1,imp-2,0,CNY\n"
        "t1,imp-3,100,XXX\n"
        "t1,imp-4,-5,CNY\n"
        "t1,imp-5,abc,CNY\n"
        "t1,,100,CNY\n"
        ",imp-6,100,CNY\n"
        "t1,imp-7,100,CNY,extra\n"
    )
    resp = submit("t1", "task-1", csv)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "completed"
    assert body["total_rows"] == 8
    assert body["success_count"] == 1
    assert body["failure_count"] == 7
    assert body["processed_count"] == 8
    assert body["completed_at"]
    lines = {e["line_number"]: e for e in body["errors"]}
    assert set(lines) == {2, 3, 4, 5, 6, 7, 8}
    assert "positive integer" in lines[2]["reason"]
    assert "unsupported currency" in lines[3]["reason"]
    assert "4 columns" in lines[8]["reason"]
    assert lines[2]["raw_line"] == "t1,imp-2,0,CNY"

    # 成功行与逐单受理的订单字段、状态一致。
    order = client.get("/orders/imp-1", headers={"X-Tenant": "t1"}).json()
    assert order == {
        "tenant": "t1", "order_id": "imp-1", "amount_cents": 1000,
        "paid_cents": 0, "currency": "CNY", "status": "accepted",
        "outstanding_cents": 1000,
    }
    # 失败行没有产生订单。
    assert client.get("/orders/imp-2", headers={"X-Tenant": "t1"}).status_code == 404


def test_duplicate_lines_only_first_accepted() -> None:
    csv = HEADER + (
        "t1,dup-1,100,CNY\n"
        "t1,dup-1,100,CNY\n"          # 完全相同重复行
        "t1,dup-1,200,CNY\n"          # 同订单标识不同内容
    )
    body = submit("t1", "task-dup", csv).json()
    assert body["success_count"] == 1
    assert body["failure_count"] == 2
    reasons = {e["line_number"]: e["reason"] for e in body["errors"]}
    assert "identical line" in reasons[2]
    assert "order_id already used" in reasons[3]
    # 只受理一次，且先到行内容为准。
    assert client.get("/orders/dup-1", headers={"X-Tenant": "t1"}).json()["amount_cents"] == 100


def test_preexisting_order_conflicts_row() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "pre-1",
                                 "amount_cents": 500, "currency": "CNY"})
    csv = HEADER + "t1,pre-1,500,CNY\nt1,pre-2,500,USD\n"
    body = submit("t1", "task-pre", csv).json()
    assert body["success_count"] == 1
    assert body["errors"][0]["reason"] == "order already exists for tenant"


def test_imported_order_works_on_all_chains() -> None:
    csv = HEADER + "t1,chain-1,1000,CNY\n"
    submit("t1", "task-chain", csv)
    h = {"X-Tenant": "t1"}
    # 收款
    assert client.post("/orders/chain-1/payments", json={"amount_cents": 1000}, headers=h).status_code == 200
    # 退款受理 + 读取
    r = client.post("/refunds", json={"refund_id": "rf-chain", "order_id": "chain-1",
                                      "amount_cents": 300, "reason": "x"},
                    headers={**h, "Idempotency-Key": "rf-chain-k"})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    assert client.get("/refunds/rf-chain", headers=h).status_code == 200
    # 结算受理 + 推进
    s = client.post("/settlements", json={"settlement_id": "st-chain", "order_id": "chain-1",
                                          "amount_cents": 300, "refund_ids": ["rf-chain"],
                                          "reason": "s"},
                    headers={**h, "Idempotency-Key": "st-chain-k"})
    assert s.status_code == 201
    assert client.post("/settlements/st-chain/advance",
                       headers={**h, "Idempotency-Key": "st-adv-k"}).status_code == 200
    # 对账
    rec = client.post("/reconciliations", json={"batch_id": "rec-chain", "order_id": "chain-1"},
                      headers={**h, "Idempotency-Key": "rec-k"})
    assert rec.status_code == 201
    assert rec.json()["conclusion"] in ("balanced", "mismatched")
    # 工单
    t = client.post("/tickets", json={"ticket_id": "tk-chain", "order_id": "chain-1", "issue": "i"},
                    headers={**h, "Idempotency-Key": "tk-k"})
    assert t.status_code == 201


def test_cross_tenant_read_imported_order_is_404() -> None:
    submit("t1", "task-xt", HEADER + "t1,xt-1,100,CNY\n")
    assert client.get("/orders/xt-1", headers={"X-Tenant": "t2"}).status_code == 404
    # 导入任务同样按租户隔离
    assert client.get("/order-imports/task-xt", headers={"X-Tenant": "t2"}).status_code == 404


def test_replay_same_content_is_idempotent() -> None:
    csv = HEADER + "t1,rp-1,100,CNY\nt1,rp-1,100,CNY\n"
    first = submit("t1", "task-rp", csv)
    assert first.status_code == 201
    second = submit("t1", "task-rp", csv)
    assert second.status_code == 201
    assert second.json() == first.json()
    # 没有第二次受理
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id='rp-1'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_same_task_different_content_conflicts() -> None:
    assert submit("t1", "task-dc", HEADER + "t1,dc-1,100,CNY\n").status_code == 201
    conflict = submit("t1", "task-dc", HEADER + "t1,dc-2,200,CNY\n")
    assert conflict.status_code == 409
    # 已存在任务与订单数据不变
    body = client.get("/order-imports/task-dc", headers={"X-Tenant": "t1"}).json()
    assert body["total_rows"] == 1
    assert client.get("/orders/dc-1", headers={"X-Tenant": "t1"}).status_code == 200
    assert client.get("/orders/dc-2", headers={"X-Tenant": "t1"}).status_code == 404


def test_get_unknown_task_is_404() -> None:
    assert client.get("/order-imports/missing", headers={"X-Tenant": "t1"}).status_code == 404


def test_bad_requests() -> None:
    assert client.post("/order-imports", json={"task_id": "x", "csv_content": HEADER + "t1,a,1,CNY\n"}
                       ).status_code == 400  # 无租户头
    assert submit("t1", "bad-1", "").status_code in (400, 422)
    assert submit("t1", "bad-2", "a,b,c,d\nx,y,1,CNY\n").status_code == 400  # 表头不符
    assert submit("t1", "bad-3", HEADER).status_code == 400  # 只有表头无数据


def test_resume_after_interruption_matches_one_shot() -> None:
    csv = HEADER + "".join(f"t1,rs-{i},{100 + i},CNY\n" for i in range(6))
    original = order_imports._insert_order
    calls = {"n": 0}

    def crashing(conn, tenant, order_id, amount_cents, currency):
        calls["n"] += 1
        if calls["n"] == 3:
            # 模拟服务在第 3 行受理后、事务提交前中断：当前行整体回滚。
            raise RuntimeError("simulated outage")
        return original(conn, tenant, order_id, amount_cents, currency)

    order_imports._insert_order = crashing
    try:
        with pytest.raises(RuntimeError):
            order_imports.accept("t1", "task-rs", csv)
    finally:
        order_imports._insert_order = original

    mid = client.get("/order-imports/task-rs", headers={"X-Tenant": "t1"}).json()
    assert mid["status"] == "pending"
    assert mid["processed_count"] == 2
    assert mid["completed_at"] is None
    # 中断的第 3 行没有留下半行结果。
    assert client.get("/orders/rs-1", headers={"X-Tenant": "t1"}).status_code == 200
    assert client.get("/orders/rs-2", headers={"X-Tenant": "t1"}).status_code == 404

    # 续跑只处理未决行。
    resumed = client.post("/order-imports/task-rs/resume", headers={"X-Tenant": "t1"})
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["status"] == "completed"
    assert (body["success_count"] + body["failure_count"]
            == body["processed_count"] == body["total_rows"] == 6)
    # 已成功行未被重复受理。
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id LIKE 'rs-%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 6

    # 再次续跑/重放返回相同结果，不产生任何变化。
    again = client.post("/order-imports/task-rs/resume", headers={"X-Tenant": "t1"}).json()
    assert again == body
    replay = submit("t1", "task-rs", csv).json()
    assert replay == body


def test_resume_unknown_task_is_404() -> None:
    r = client.post("/order-imports/nope/resume", headers={"X-Tenant": "t1"})
    assert r.status_code == 404


def test_replay_after_interruption_continues_task() -> None:
    csv = HEADER + "t1,rr-1,100,CNY\nt1,rr-2,200,CNY\nt1,rr-3,300,CNY\n"
    original = order_imports._insert_order
    calls = {"n": 0}

    def crashing(conn, tenant, order_id, amount_cents, currency):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated outage")
        return original(conn, tenant, order_id, amount_cents, currency)

    order_imports._insert_order = crashing
    try:
        with pytest.raises(RuntimeError):
            order_imports.accept("t1", "task-rr", csv)
    finally:
        order_imports._insert_order = original

    # 以同一任务标识、同一内容再次“提交”即识别为重放并续跑。
    body = submit("t1", "task-rr", csv).json()
    assert body["status"] == "completed"
    assert body["success_count"] == 3
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE tenant='t1' AND order_id LIKE 'rr-%'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 3


def test_concurrent_different_content_only_one_takes_effect() -> None:
    csv_a = HEADER + "t1,ca-1,100,CNY\nt1,ca-2,100,CNY\n"
    csv_b = HEADER + "t1,cb-1,200,CNY\n"
    outcomes: list[order_imports.Outcome] = []
    barrier = threading.Barrier(2)

    def worker(content: str) -> None:
        barrier.wait()
        outcomes.append(order_imports.accept("t1", "task-cc", content))

    t1 = threading.Thread(target=worker, args=(csv_a,))
    t2 = threading.Thread(target=worker, args=(csv_b,))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(o.status for o in outcomes)
    assert statuses == [201, 409]
    body = client.get("/order-imports/task-cc", headers={"X-Tenant": "t1"}).json()
    # 先到者生效（A 两单或 B 一单），失败者的数据未落地。
    if body["total_rows"] == 2:
        assert client.get("/orders/ca-1", headers={"X-Tenant": "t1"}).status_code == 200
        assert client.get("/orders/ca-2", headers={"X-Tenant": "t1"}).status_code == 200
        assert client.get("/orders/cb-1", headers={"X-Tenant": "t1"}).status_code == 404
    else:
        assert body["total_rows"] == 1
        assert client.get("/orders/cb-1", headers={"X-Tenant": "t1"}).status_code == 200
        assert client.get("/orders/ca-1", headers={"X-Tenant": "t1"}).status_code == 404


def test_internal_failure_returns_500() -> None:
    crashing_client = TestClient(app, raise_server_exceptions=False)
    original = order_imports._insert_order

    def boom(conn, tenant, order_id, amount_cents, currency):
        raise RuntimeError("boom")

    order_imports._insert_order = boom
    try:
        resp = crashing_client.post(
            "/order-imports", headers={"X-Tenant": "t1"},
            json={"task_id": "task-500", "csv_content": HEADER + "t1,e500-1,100,CNY\n"},
        )
        assert resp.status_code == 500
    finally:
        order_imports._insert_order = original

    # 中断后任务保持 pending，可续跑成功。
    resumed = client.post("/order-imports/task-500/resume", headers={"X-Tenant": "t1"})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert client.get("/orders/e500-1", headers={"X-Tenant": "t1"}).status_code == 200
