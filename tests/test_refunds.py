import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_refunds.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import refunds as refund_store
from app.store.db import connect, migrate

# 与 tests/test_orders.py（租户 t1）共库时用独立租户隔离，避免单据标识冲突。
T = "rt"
migrate()
client = TestClient(app)

H = {"X-Tenant": T}


def _order(tenant: str = T, order_id: str = "r0", amount: int = 1000) -> None:
    r = client.post("/orders", json={"tenant": tenant, "order_id": order_id,
                                     "amount_cents": amount, "currency": "CNY"})
    assert r.status_code == 201, r.text


def _pay(order_id: str, amount: int, tenant: str = T) -> None:
    r = client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount},
                    headers={"X-Tenant": tenant})
    assert r.status_code == 200, r.text


def _accept(refund_id: str, order_id: str, amount: int, key: str, *,
            reason: str = "customer request", tenant: str = T):
    return client.post("/refunds",
                       json={"refund_id": refund_id, "order_id": order_id,
                             "amount_cents": amount, "reason": reason},
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


# ---------- 受理与读取 ----------

def test_accept_refund_and_read_back() -> None:
    _order(order_id="o1")
    _pay("o1", 600)
    r = _accept("rf1", "o1", 400, "k1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"refund_id", "order_id", "amount_cents", "reason", "status", "created_at"}
    assert body["refund_id"] == "rf1" and body["order_id"] == "o1"
    assert body["amount_cents"] == 400 and body["status"] == "pending"
    got = client.get("/refunds/rf1", headers=H)
    assert got.status_code == 200 and got.json() == body


def test_refund_requires_idempotency_key() -> None:
    _order(order_id="o1k")
    _pay("o1k", 100)
    r = client.post("/refunds",
                    json={"refund_id": "rfk", "order_id": "o1k", "amount_cents": 10, "reason": "x"},
                    headers=H)
    assert r.status_code == 400


def test_refund_amount_must_be_positive_integer() -> None:
    _order(order_id="o2")
    _pay("o2", 500)
    assert _accept("rf2a", "o2", 0, "k2a").status_code == 400
    assert _accept("rf2b", "o2", -5, "k2b").status_code == 400
    r = client.post("/refunds",
                    json={"refund_id": "rf2c", "order_id": "o2", "amount_cents": 1.5, "reason": "x"},
                    headers={**H, "Idempotency-Key": "k2c"})
    assert r.status_code == 400


def test_refund_over_order_amount_is_invalid() -> None:
    _order(order_id="o3", amount=500)
    _pay("o3", 500)
    # 超过订单金额 → 参数不合法 400（即使已全额收款）
    assert _accept("rf3", "o3", 600, "k3").status_code == 400


def test_refund_over_refundable_is_quota_conflict() -> None:
    _order(order_id="o4", amount=1000)
    _pay("o4", 200)
    # 未收款时可退为 0
    _order(order_id="o4b", amount=1000)
    assert _accept("rf4b", "o4b", 1, "k4b").status_code == 409
    # 不超过订单金额，但超过 可退=已收−已占用 → 额度越界 409
    assert _accept("rf4", "o4", 300, "k4").status_code == 409


def test_missing_or_cross_tenant_order_is_not_found() -> None:
    assert _accept("rf5", "ghost", 10, "k5").status_code == 404
    _order(tenant=T, order_id="o6")
    _pay("o6", 100)
    assert _accept("rf6", "o6", 50, "k6", tenant="other").status_code == 404


def test_cross_tenant_refund_read_is_not_found() -> None:
    _order(order_id="o7")
    _pay("o7", 100)
    assert _accept("rf7", "o7", 100, "k7").status_code == 201
    assert client.get("/refunds/rf7", headers={"X-Tenant": "other"}).status_code == 404


# ---------- 冲正 ----------

def test_reverse_releases_capacity_and_allows_reaccept() -> None:
    _order(order_id="o8", amount=500)
    _pay("o8", 500)
    assert _accept("rf8", "o8", 500, "k8").status_code == 201
    # 额度占满后再受理被拒
    assert _accept("rf8b", "o8", 1, "k8b").status_code == 409
    r = client.post("/refunds/rf8/reversals", headers={**H, "Idempotency-Key": "rk8"})
    assert r.status_code == 200 and r.json()["status"] == "reversed"
    # 额度立即释放：同额可再次受理
    assert _accept("rf8c", "o8", 500, "k8c").status_code == 201


def test_double_reverse_conflicts_and_keeps_state() -> None:
    _order(order_id="o9")
    _pay("o9", 100)
    _accept("rf9", "o9", 100, "k9")
    assert client.post("/refunds/rf9/reversals", headers={**H, "Idempotency-Key": "rk9"}).status_code == 200
    r = client.post("/refunds/rf9/reversals", headers={**H, "Idempotency-Key": "rk9b"})
    assert r.status_code == 409
    assert client.get("/refunds/rf9", headers=H).json()["status"] == "reversed"


def test_reverse_unknown_refund_is_not_found() -> None:
    r = client.post("/refunds/ghost/reversals", headers={**H, "Idempotency-Key": "rkx"})
    assert r.status_code == 404


# ---------- 幂等重放 ----------

def test_accept_replay_returns_first_result_without_second_accept() -> None:
    _order(order_id="o10", amount=300)
    _pay("o10", 300)
    first = _accept("rf10", "o10", 300, "same-key")
    replay = _accept("rf10", "o10", 300, "same-key")
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM refunds WHERE refund_id='rf10'").fetchone()["c"]
    finally:
        conn.close()
    assert count == 1
    # 额度未被第二次扣减：已占 300，新退款必须被拒
    assert _accept("rf10b", "o10", 1, "k10b").status_code == 409


def test_different_fingerprints_same_target_only_one_wins() -> None:
    _order(order_id="o11", amount=300)
    _pay("o11", 300)
    assert _accept("rf11", "o11", 300, "key-a").status_code == 201
    # 同一目标标识、不同请求指纹 → 拒绝且不改变已存在数据
    other = _accept("rf11", "o11", 300, "key-b", reason="changed mind")
    assert other.status_code == 409
    assert client.get("/refunds/rf11", headers=H).json()["reason"] == "customer request"


def test_replay_persists_across_restart() -> None:
    _order(order_id="o12", amount=200)
    _pay("o12", 200)
    first = _accept("rf12", "o12", 200, "persist-key")
    assert first.status_code == 201
    # 模拟服务重启：新建客户端，数据库不变
    restarted = TestClient(app)
    replay = restarted.post("/refunds",
                            json={"refund_id": "rf12", "order_id": "o12",
                                  "amount_cents": 200, "reason": "customer request"},
                            headers={**H, "Idempotency-Key": "persist-key"})
    assert replay.status_code == 201 and replay.json() == first.json()
    rev = restarted.post("/refunds/rf12/reversals", headers={**H, "Idempotency-Key": "rk12"})
    assert rev.status_code == 200
    rev_again = restarted.post("/refunds/rf12/reversals", headers={**H, "Idempotency-Key": "rk12"})
    assert rev_again.status_code == 200 and rev_again.json() == rev.json()


def test_failed_request_is_replayed_too() -> None:
    # 首次因订单不存在而 404；同指纹重放仍是同一个 404，不会因订单后建而变成受理
    first = _accept("rf13", "o13", 10, "k13")
    assert first.status_code == 404
    _order(order_id="o13")
    _pay("o13", 100)
    assert _accept("rf13", "o13", 10, "k13").status_code == 404


# ---------- 并发：不同指纹同一目标/同一额度 ----------

def test_concurrent_same_refund_id_only_one_accepted() -> None:
    _order(order_id="o14", amount=1000)
    _pay("o14", 1000)
    results: list[int] = []

    def worker(key: str) -> None:
        try:
            r = refund_store.accept(T, "rf14", "o14", 1000, "x", key)
            results.append(201 if isinstance(r, dict) else r.status_code)
        except refund_store.ApiError as error:
            results.append(error.status_code)

    threads = [threading.Thread(target=worker, args=(f"cc-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 7


def test_concurrent_capacity_race_only_one_held() -> None:
    _order(order_id="o15", amount=1000)
    _pay("o15", 500)
    outcomes: list[int] = []

    def worker(i: int) -> None:
        try:
            r = refund_store.accept(T, f"rf15-{i}", "o15", 500, "x", f"ck-{i}")
            outcomes.append(201 if isinstance(r, dict) else r.status_code)
        except refund_store.ApiError as error:
            outcomes.append(error.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(201) == 1 and outcomes.count(409) == 5
    conn = connect()
    try:
        held = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds"
            " WHERE order_id='o15' AND status IN ('pending','effective')"
        ).fetchone()["s"]
    finally:
        conn.close()
    assert held == 500  # 无部分占用


# ---------- 守恒 ----------

def test_refundable_conservation_across_sequence() -> None:
    _order(order_id="o16", amount=1000)
    _pay("o16", 800)

    def held() -> int:
        conn = connect()
        try:
            return conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds"
                " WHERE tenant=? AND order_id='o16' AND status IN ('pending','effective')",
                (T,),
            ).fetchone()["s"]
        finally:
            conn.close()

    assert _accept("rf16a", "o16", 500, "k16a").status_code == 201
    assert held() == 500
    assert _accept("rf16b", "o16", 400, "k16b").status_code == 409  # 800-500=300 < 400
    assert _accept("rf16b", "o16", 300, "k16b").status_code == 201
    assert held() == 800
    client.post("/refunds/rf16a/reversals", headers={**H, "Idempotency-Key": "rk16a"})
    assert held() == 300  # 冲正立即释放
    assert _accept("rf16c", "o16", 500, "k16c").status_code == 201
    assert held() == 800
    # 任意序列后：0 <= 已收 − 已生效(占用)退款合计 <= 订单金额，且合计不超过已收
    assert 0 <= 800 - held() <= 1000 and held() <= 800
