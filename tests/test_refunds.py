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
# Dedicated tenant so this module's fixtures never collide with tests/test_orders.py
TENANT = "rt"
H = {"X-Tenant": TENANT}


def _make_paid_order(order_id: str, amount: int = 1000, paid: int | None = None) -> None:
    order_store.insert(TENANT, order_id, amount, "CNY")
    order_store.add_payment(TENANT, order_id, paid if paid is not None else amount)


def _refund(refund_id: str, order_id: str, amount: int, key: str,
            reason: str = "customer request", tenant: str = TENANT):
    return client.post(
        "/refunds",
        json={"refund_id": refund_id, "order_id": order_id,
              "amount_cents": amount, "reason": reason},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _reverse(refund_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/refunds/{refund_id}/reverse",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


# ---------- accept & read ----------

def test_accept_refund_is_pending_and_returns_fields() -> None:
    _make_paid_order("o1", 1000)
    resp = _refund("r1", "o1", 300, "k1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"refund_id", "order_id", "amount_cents", "reason", "status", "created_at"}
    assert body["refund_id"] == "r1" and body["order_id"] == "o1"
    assert body["amount_cents"] == 300 and body["status"] == "pending" and body["reason"]

    got = client.get("/refunds/r1", headers=H)
    assert got.status_code == 200 and got.json() == body


def test_read_is_tenant_isolated() -> None:
    _make_paid_order("o2", 1000)
    _refund("r2", "o2", 100, "k2")
    assert client.get("/refunds/r2", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/refunds/r2").status_code == 400


def test_accept_against_missing_or_cross_tenant_order_is_not_found() -> None:
    assert _refund("r3", "no-such-order", 10, "k3").status_code == 404
    # out-of-bounds amount on a non-existent order still reads as "not found"
    assert _refund("r3b", "no-such-order", 10_000_000, "k3b").status_code == 404

    order_store.insert("t2", "foreign-order", 500, "CNY")
    order_store.add_payment("t2", "foreign-order", 500)
    assert _refund("r3c", "foreign-order", 10, "k3c", tenant="t1").status_code == 404


def test_amount_must_be_positive_integer() -> None:
    _make_paid_order("o4", 1000)
    # non-positive / non-integer rejected at the request boundary
    bad = client.post("/refunds",
                      json={"refund_id": "r4", "order_id": "o4", "amount_cents": 0, "reason": ""},
                      headers={**H, "Idempotency-Key": "k4"})
    assert bad.status_code == 422
    assert client.get("/refunds/r4", headers=H).status_code == 404


def test_refund_cannot_exceed_order_amount_or_refundable_quota() -> None:
    _make_paid_order("o5", 1000, paid=400)
    # more than the order amount is an invalid parameter
    assert _refund("r5a", "o5", 1200, "k5a").status_code == 400
    # within the order but beyond paid - occupied is a quota breach
    assert _refund("r5b", "o5", 500, "k5b").status_code == 409
    ok = _refund("r5c", "o5", 400, "k5c")
    assert ok.status_code == 201
    # quota fully occupied now
    assert _refund("r5d", "o5", 1, "k5d").status_code == 409


# ---------- quota conservation & reversal ----------

def test_refundable_formula_conservation() -> None:
    _make_paid_order("o6", 1000)
    assert _refund("r6a", "o6", 300, "k6a").status_code == 201
    assert _refund("r6b", "o6", 300, "k6b").status_code == 201
    assert _refund("r6c", "o6", 401, "k6c").status_code == 409  # only 400 left

    # reverse one pending refund: quota released, same amount can be accepted again
    rev = _reverse("r6a", "rk6a")
    assert rev.status_code == 200 and rev.json()["status"] == "reversed"
    assert _refund("r6d", "o6", 400, "k6d").status_code == 201

    conn = connect()
    try:
        order = conn.execute("SELECT amount_cents, paid_cents FROM orders WHERE order_id='o6'").fetchone()
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
            "WHERE order_id='o6' AND status IN ('pending','effective')").fetchone()["s"]
        rows = conn.execute("SELECT COUNT(*) c FROM refunds WHERE order_id='o6'").fetchone()["c"]
    finally:
        conn.close()
    # conservation: occupied (pending/effective) never exceeds paid, and the formula holds
    assert occupied == 700
    assert order["paid_cents"] - occupied == 300
    assert occupied <= order["paid_cents"] <= order["amount_cents"]
    assert rows == 3  # accepted rows (r6a,r6b,r6d); the rejected r6c created no row


def test_reverse_effective_refund_releases_quota() -> None:
    _make_paid_order("o7", 500)
    _refund("r7", "o7", 500, "k7")
    # simulate the downstream settlement marking the refund effective
    conn = connect()
    try:
        conn.execute("UPDATE refunds SET status='effective' WHERE refund_id='r7'")
        conn.commit()
    finally:
        conn.close()
    assert _reverse("r7", "rk7").status_code == 200
    assert client.get("/refunds/r7", headers=H).json()["status"] == "reversed"
    # full quota available again
    assert _refund("r7b", "o7", 500, "k7b").status_code == 201


def test_repeat_reverse_conflicts_and_keeps_state() -> None:
    _make_paid_order("o8", 200)
    _refund("r8", "o8", 200, "k8")
    assert _reverse("r8", "rk8a").status_code == 200
    again = _reverse("r8", "rk8b")  # different request, same target
    assert again.status_code == 409
    assert client.get("/refunds/r8", headers=H).json()["status"] == "reversed"


def test_reverse_missing_is_not_found() -> None:
    assert _reverse("ghost", "rkghost").status_code == 404
    _make_paid_order("o9", 100)
    _refund("r9", "o9", 10, "k9")
    assert _reverse("r9", "rk9", tenant="t2").status_code == 404


# ---------- idempotent replay ----------

def test_accept_replay_returns_first_result_and_occupies_quota_once() -> None:
    _make_paid_order("o10", 100)
    first = _refund("r10", "o10", 100, "same-key")
    replay = _refund("r10", "o10", 100, "same-key")
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()

    # only one refund row; quota occupied exactly once
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM refunds WHERE refund_id='r10'").fetchone()["c"]
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
            "WHERE order_id='o10' AND status IN ('pending','effective')").fetchone()["s"]
    finally:
        conn.close()
    assert n == 1 and occupied == 100


def test_replay_succeeds_even_after_quota_moves_and_reversal() -> None:
    _make_paid_order("o11", 100)
    first = _refund("r11", "o11", 100, "k11")
    assert first.status_code == 201
    _reverse("r11", "rk11")  # quota released and reused by another refund
    assert _refund("r11b", "o11", 100, "k11b").status_code == 201
    # original request replayed -> identical original result, no second acceptance/error
    replay = _refund("r11", "o11", 100, "k11")
    assert replay.status_code == 201
    assert replay.json()["status"] == "pending"
    assert replay.json()["created_at"] == first.json()["created_at"]


def test_distinct_fingerprints_same_target_only_one_wins() -> None:
    _make_paid_order("o12", 100)
    codes = []

    def run(key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_refund("r12", "o12", 100, key).status_code)

    barrier = threading.Barrier(5)
    threads = [threading.Thread(target=run, args=(f"k12-{i}", barrier)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(201) == 1
    assert sorted(codes).count(409) == 4
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM refunds WHERE refund_id='r12'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_reverse_replay_releases_once() -> None:
    _make_paid_order("o13", 100)
    _refund("r13", "o13", 100, "k13")
    a = _reverse("r13", "same-rev-key")
    b = _reverse("r13", "same-rev-key")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests WHERE target_id='r13' AND operation='reverse'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_failed_result_is_replayed_identically() -> None:
    # a first request that targets a missing order is remembered as 404 ...
    first = _refund("r14", "missing-order", 10, "k14")
    assert first.status_code == 404
    _make_paid_order("missing-order", 100)
    # ... even after the order later exists, the same fingerprint replays as 404
    replay = _refund("r14", "missing-order", 10, "k14")
    assert replay.status_code == 404
    # a different fingerprint against the now-existing order succeeds normally
    assert _refund("r14b", "missing-order", 10, "k14b").status_code == 201


# ---------- durability across restart ----------

def test_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders, refunds\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('rt','ro',1000,'CNY')\n"
        "    orders.add_payment('rt','ro',1000)\n"
        "out = refunds.accept('rt','rr','ro',250,'why','persist-key')\n"
        "conn = connect()\n"
        "n = conn.execute(\"SELECT COUNT(*) FROM refunds WHERE refund_id='rr'\").fetchone()[0]\n"
        "conn.close()\n"
        "print(json.dumps({'status': out.status, 'body_status': out.body.get('status'), 'n': n}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a["status"] == b["status"] == 201
    assert a["body_status"] == b["body_status"] == "pending"
    assert a["n"] == b["n"] == 1  # replay, not a second acceptance
