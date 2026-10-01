import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
# Dedicated tenant so this module's fixtures never collide with the other suites.
TENANT = "wo"
H = {"X-Tenant": TENANT}


def _make_paid_order(order_id: str, amount: int = 1000, paid: int | None = None,
                     tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")
    order_store.add_payment(tenant, order_id, paid if paid is not None else amount)


def _accept(work_order_id: str, order_id: str, key: str, issue: str = "客户反馈异常",
            tenant: str = TENANT):
    return client.post(
        "/work-orders",
        json={"work_order_id": work_order_id, "order_id": order_id, "issue": issue},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _op(path: str, key: str, tenant: str = TENANT, json_body=None):
    return client.post(path, json=json_body,
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _reconcile(batch_id: str, order_id: str, key: str, tenant: str = TENANT,
               note: str = "月度对账"):
    return client.post(
        "/reconciliations",
        json={"batch_id": batch_id, "order_id": order_id, "note": note},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _insert_batch(batch_id: str, order_id: str, conclusion: str,
                  tenant: str = TENANT) -> None:
    """Insert a completed reconciliation batch directly (audit-style fixture)."""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO reconciliations(tenant, batch_id, order_id, note, paid_cents, "
            "pending_refund_cents, effective_refund_cents, effective_settlement_cents, "
            "settled_balance_cents, conclusion, status, created_at, completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant, batch_id, order_id, "", 0, 0, 0, 0, 0, conclusion, "completed",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"),
        )
    finally:
        conn.close()


# ---------- accept & read ----------

def test_accept_registers_pending_work_order_and_read_back() -> None:
    _make_paid_order("woo1")
    resp = _accept("w1", "woo1", "w1-accept", issue="少发货")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"work_order_id", "order_id", "issue", "resolution",
                         "status", "created_at", "updated_at"}
    assert body["work_order_id"] == "w1"
    assert body["order_id"] == "woo1"
    assert body["issue"] == "少发货"
    assert body["resolution"] == ""
    assert body["status"] == "pending"
    assert body["created_at"] == body["updated_at"]

    got = client.get("/work-orders/w1", headers=H)
    assert got.status_code == 200 and got.json() == body


def test_read_work_order_is_tenant_isolated() -> None:
    _make_paid_order("woo2")
    _accept("w2", "woo2", "w2-accept")
    assert client.get("/work-orders/w2", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/work-orders/w2").status_code == 400


def test_accept_missing_or_cross_tenant_order_is_not_found_without_leak() -> None:
    resp = _accept("w3a", "woo3-ghost", "w3a-accept")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "work order target not found"

    _make_paid_order("foreign-o3", 500, tenant="t2")
    cross = _accept("w3b", "foreign-o3", "w3b-accept", tenant="t1")
    assert cross.status_code == 404
    # no object existence leaked: neither work order was created
    assert client.get("/work-orders/w3a", headers=H).status_code == 404
    assert client.get("/work-orders/w3b", headers={"X-Tenant": "t1"}).status_code == 404


def test_missing_headers_are_rejected() -> None:
    _make_paid_order("woo4")
    no_tenant = client.post("/work-orders",
                            json={"work_order_id": "w4", "order_id": "woo4", "issue": "x"},
                            headers={"Idempotency-Key": "w4-accept"})
    assert no_tenant.status_code == 400
    no_key = client.post("/work-orders",
                         json={"work_order_id": "w4", "order_id": "woo4", "issue": "x"},
                         headers=H)
    assert no_key.status_code == 400
    missing_fields = client.post("/work-orders", json={"work_order_id": "w4"},
                                 headers={**H, "Idempotency-Key": "w4-accept"})
    assert missing_fields.status_code == 422
    assert client.get("/work-orders/w4", headers=H).status_code == 404


def test_duplicate_work_order_id_conflicts_and_keeps_first() -> None:
    _make_paid_order("woo5")
    first = _accept("w5", "woo5", "w5-accept-a", issue="第一个问题")
    assert first.status_code == 201
    dup = _accept("w5", "woo5", "w5-accept-b", issue="另一个问题")
    assert dup.status_code == 409
    assert dup.json()["detail"] == "work order already accepted"
    kept = client.get("/work-orders/w5", headers=H).json()
    assert kept == first.json()
    assert kept["issue"] == "第一个问题"


# ---------- state machine ----------

def test_full_lifecycle_pending_to_closed() -> None:
    _make_paid_order("woo6")
    assert _accept("w6", "woo6", "w6-accept").status_code == 201

    processed = _op("/work-orders/w6/process", "w6-proc")
    assert processed.status_code == 200
    assert processed.json()["status"] == "in_progress"
    assert processed.json()["updated_at"] >= processed.json()["created_at"]

    resolved = _op("/work-orders/w6/resolve", "w6-res", json_body={"resolution": "已补发"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolution"] == "已补发"
    assert client.get("/work-orders/w6", headers=H).json()["resolution"] == "已补发"

    closed = _op("/work-orders/w6/close", "w6-close")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"


def test_process_is_idempotent_on_in_progress_but_resolved_and_closed_conflict() -> None:
    _make_paid_order("woo7")
    _accept("w7", "woo7", "w7-accept")
    assert _op("/work-orders/w7/process", "w7-proc-1").json()["status"] == "in_progress"
    # processing an in-progress ticket stays in_progress (no state change)
    again = _op("/work-orders/w7/process", "w7-proc-2")
    assert again.status_code == 200 and again.json()["status"] == "in_progress"

    assert _op("/work-orders/w7/resolve", "w7-res",
               json_body={"resolution": "done"}).json()["status"] == "resolved"
    conflict = _op("/work-orders/w7/process", "w7-proc-3")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "work order cannot be processed from its current status"
    assert client.get("/work-orders/w7", headers=H).json()["status"] == "resolved"

    assert _op("/work-orders/w7/close", "w7-close").status_code == 200
    closed_conflict = _op("/work-orders/w7/process", "w7-proc-4")
    assert closed_conflict.status_code == 409
    assert client.get("/work-orders/w7", headers=H).json()["status"] == "closed"


def test_resolve_and_close_illegal_transitions_leave_state_untouched() -> None:
    _make_paid_order("woo8")
    _accept("w8", "woo8", "w8-accept")
    assert _op("/work-orders/w8/close", "w8-close-1").json()["status"] == "closed"

    resolve_closed = _op("/work-orders/w8/resolve", "w8-res",
                         json_body={"resolution": "late"})
    assert resolve_closed.status_code == 409
    assert "cannot be resolved" in resolve_closed.json()["detail"]
    re_close = _op("/work-orders/w8/close", "w8-close-2")
    assert re_close.status_code == 409
    assert re_close.json()["detail"] == "work order already closed"
    body = client.get("/work-orders/w8", headers=H).json()
    assert body["status"] == "closed" and body["resolution"] == ""


def test_close_allowed_from_every_non_terminal_state() -> None:
    _make_paid_order("woo9a")
    _accept("w9a", "woo9a", "w9a-accept")
    assert _op("/work-orders/w9a/close", "w9a-close").json()["status"] == "closed"

    _make_paid_order("woo9b")
    _accept("w9b", "woo9b", "w9b-accept")
    _op("/work-orders/w9b/process", "w9b-proc")
    assert _op("/work-orders/w9b/close", "w9b-close").json()["status"] == "closed"


def test_state_op_on_missing_or_cross_tenant_work_order_is_404() -> None:
    resp = _op("/work-orders/ghost/process", "ghost-proc")
    assert resp.status_code == 404 and resp.json()["detail"] == "work order not found"
    _make_paid_order("woo10")
    _accept("w10", "woo10", "w10-accept")
    cross = _op("/work-orders/w10/process", "w10-proc", tenant="t2")
    assert cross.status_code == 404
    assert client.get("/work-orders/w10", headers=H).json()["status"] == "pending"


# ---------- mismatched reconciliation guard ----------

def test_mismatched_completed_reconciliation_blocks_resolve() -> None:
    _make_paid_order("woo11")
    _accept("w11", "woo11", "w11-accept")
    _op("/work-orders/w11/process", "w11-proc")
    _insert_batch("w11-bad", "woo11", "mismatched")

    blocked = _op("/work-orders/w11/resolve", "w11-res-1",
                  json_body={"resolution": "想直接解决"})
    assert blocked.status_code == 409
    assert "mismatched completed reconciliation" in blocked.json()["detail"]
    body = client.get("/work-orders/w11", headers=H).json()
    assert body["status"] == "in_progress" and body["resolution"] == ""


def test_new_balanced_batch_reopens_resolve() -> None:
    _make_paid_order("woo12")
    _accept("w12", "woo12", "w12-accept")
    _insert_batch("w12-bad", "woo12", "mismatched")
    assert _op("/work-orders/w12/resolve", "w12-res-blocked",
               json_body={"resolution": "x"}).status_code == 409

    # re-reconcile with a fresh batch; fully paid order reconciles balanced
    assert _reconcile("w12-good", "woo12", "w12-rec").status_code == 201
    ok = _op("/work-orders/w12/resolve", "w12-res-ok",
             json_body={"resolution": "对账相符后解决"})
    assert ok.status_code == 200 and ok.json()["status"] == "resolved"


def test_closed_work_order_is_not_blocked_by_mismatched_reconciliation() -> None:
    _make_paid_order("woo13")
    _accept("w13", "woo13", "w13-accept")
    _insert_batch("w13-bad", "woo13", "mismatched")
    # closing bypasses the reconciliation gate entirely
    closed = _op("/work-orders/w13/close", "w13-close")
    assert closed.status_code == 200 and closed.json()["status"] == "closed"


def test_mismatched_block_does_not_affect_other_work_orders() -> None:
    _make_paid_order("woo14a")
    _make_paid_order("woo14b")
    _accept("w14a", "woo14a", "w14a-accept")
    _accept("w14b", "woo14b", "w14b-accept")
    _insert_batch("w14a-bad", "woo14a", "mismatched")
    assert _op("/work-orders/w14a/resolve", "w14a-res",
               json_body={"resolution": "x"}).status_code == 409
    ok = _op("/work-orders/w14b/resolve", "w14b-res",
             json_body={"resolution": "另一单正常解决"})
    assert ok.status_code == 200 and ok.json()["status"] == "resolved"


# ---------- idempotent replay ----------

def test_accept_replay_returns_first_result_including_errors() -> None:
    first = _accept("w15", "woo15-ghost", "w15-same-key")
    assert first.status_code == 404
    _make_paid_order("woo15-ghost", 500)
    replay = _accept("w15", "woo15-ghost", "w15-same-key")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert client.get("/work-orders/w15", headers=H).status_code == 404
    # a different fingerprint re-validates against current state and succeeds
    assert _accept("w15", "woo15-ghost", "w15-retry").status_code == 201


def test_state_flip_replay_is_identical_and_records_once() -> None:
    _make_paid_order("woo16")
    _accept("w16", "woo16", "w16-accept")
    a = _op("/work-orders/w16/resolve", "same-res-key",
            json_body={"resolution": "首次说明"})
    assert a.status_code == 200 and a.json()["status"] == "resolved"
    # replaying after the ticket moved on still returns the first result
    assert _op("/work-orders/w16/close", "w16-close").status_code == 200
    b = _op("/work-orders/w16/resolve", "same-res-key",
            json_body={"resolution": "首次说明"})
    assert a.status_code == b.status_code and a.json() == b.json()

    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='w16' AND operation='work_order_resolve'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_failed_resolve_replays_old_conflict_even_after_balance_restored() -> None:
    _make_paid_order("woo17")
    _accept("w17", "woo17", "w17-accept")
    _insert_batch("w17-bad", "woo17", "mismatched")
    first = _op("/work-orders/w17/resolve", "w17-same-res",
                json_body={"resolution": "x"})
    assert first.status_code == 409
    _reconcile("w17-good", "woo17", "w17-rec")
    replay = _op("/work-orders/w17/resolve", "w17-same-res",
                 json_body={"resolution": "x"})
    assert replay.status_code == 409 and replay.json() == first.json()
    assert client.get("/work-orders/w17", headers=H).json()["status"] == "pending"
    assert _op("/work-orders/w17/resolve", "w17-new-res",
               json_body={"resolution": "x"}).status_code == 200


# ---------- concurrency ----------

def test_concurrent_closes_different_keys_single_winner() -> None:
    _make_paid_order("woo18")
    _accept("w18", "woo18", "w18-accept")
    results: list[int] = []

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _op("/work-orders/w18/close", f"w18-close-{i}")
        results.append(resp.status_code)

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count(200) == 1
    assert sorted(results).count(409) == 5
    assert client.get("/work-orders/w18", headers=H).json()["status"] == "closed"


def test_concurrent_same_fingerprint_flips_once_and_all_replay_it() -> None:
    _make_paid_order("woo19")
    _accept("w19", "woo19", "w19-accept")
    results: list[int] = []

    def run(barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _op("/work-orders/w19/close", "shared-close-key")
        results.append(resp.status_code)

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(barrier,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [200] * 6
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='w19' AND operation='work_order_close' "
            "AND request_fingerprint='shared-close-key'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


# ---------- no side effects on other documents ----------

def test_work_order_life_cycle_does_not_touch_other_documents() -> None:
    _make_paid_order("woo20", 1000, paid=800)
    client.post("/refunds",
                json={"refund_id": "w20-r1", "order_id": "woo20",
                      "amount_cents": 100, "reason": "customer request"},
                headers={**H, "Idempotency-Key": "w20-r1"})
    client.post("/settlements",
                json={"settlement_id": "w20-s1", "order_id": "woo20",
                      "amount_cents": 100, "refund_ids": ["w20-r1"], "reason": "周期结算"},
                headers={**H, "Idempotency-Key": "w20-s1-accept"})
    client.post("/settlements/w20-s1/advance",
                headers={**H, "Idempotency-Key": "w20-s1-adv"})
    _reconcile("w20-b1", "woo20", "w20-rec")

    before_order = client.get("/orders/woo20", headers=H).json()
    before_refund = client.get("/refunds/w20-r1", headers=H).json()
    before_settlement = client.get("/settlements/w20-s1", headers=H).json()
    before_batch = client.get("/reconciliations/w20-b1", headers=H).json()

    _accept("w20", "woo20", "w20-accept", issue="问题")
    _op("/work-orders/w20/process", "w20-proc")
    _op("/work-orders/w20/resolve", "w20-res", json_body={"resolution": "处理完毕"})
    _op("/work-orders/w20/close", "w20-close")

    assert client.get("/orders/woo20", headers=H).json() == before_order
    assert client.get("/refunds/w20-r1", headers=H).json() == before_refund
    assert client.get("/settlements/w20-s1", headers=H).json() == before_settlement
    assert client.get("/reconciliations/w20-b1", headers=H).json() == before_batch


# ---------- durability across restart ----------

def test_work_order_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-work-order.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import migrate\n"
        "from app.store import orders, work_orders\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('wo','ro',1000,'CNY')\n"
        "    work_orders.accept('wo','rw','ro','异常','wo-key')\n"
        "out = work_orders.resolve('wo','rw','已解决','res-key')\n"
        "print(json.dumps({'http': out.status, 'code': out.code, 'body': out.body}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b
    assert a["http"] == 200
    assert a["body"]["status"] == "resolved"
    assert a["body"]["resolution"] == "已解决"
