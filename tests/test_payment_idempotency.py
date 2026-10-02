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
# Dedicated tenant so this module's fixtures never collide with other test modules
TENANT = "pt"
H = {"X-Tenant": TENANT}


def _make_order(order_id: str, amount: int = 1000, tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")


def _pay(order_id: str, amount: int, key: str, tenant: str = TENANT):
    return client.post(f"/orders/{order_id}/payments",
                       json={"amount_cents": amount},
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _paid_cents(order_id: str) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (TENANT, order_id)).fetchone()["paid_cents"]
    finally:
        conn.close()


def _recorded_outcomes(order_id: str) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE tenant=? AND operation='payment_accept' AND target_id=?",
            (TENANT, order_id)).fetchone()["c"]
    finally:
        conn.close()


# ---------- parameter validation ----------

def test_missing_idempotency_key_is_rejected_and_records_nothing() -> None:
    _make_order("p1", 500)
    resp = client.post("/orders/p1/payments", json={"amount_cents": 100}, headers=H)
    assert resp.status_code == 400
    assert _paid_cents("p1") == 0 and _recorded_outcomes("p1") == 0


def test_missing_tenant_header_is_rejected() -> None:
    _make_order("p2", 500)
    resp = client.post("/orders/p2/payments", json={"amount_cents": 100},
                       headers={"Idempotency-Key": "p2-k"})
    assert resp.status_code == 400
    assert _paid_cents("p2") == 0


def test_non_positive_amount_is_rejected_and_records_nothing() -> None:
    _make_order("p3", 500)
    for bad in (0, -100):
        resp = _pay("p3", bad, f"p3-k{bad}")
        assert resp.status_code == 422
    assert _paid_cents("p3") == 0 and _recorded_outcomes("p3") == 0


def test_missing_or_cross_tenant_order_is_not_found() -> None:
    assert _pay("ghost-order", 100, "p4-k1").status_code == 404
    _make_order("p4-foreign", 500, tenant="other-tenant")
    assert _pay("p4-foreign", 100, "p4-k2").status_code == 404
    # no payment was registered on the foreign order
    conn = connect()
    try:
        paid = conn.execute(
            "SELECT paid_cents FROM orders WHERE tenant='other-tenant' AND order_id='p4-foreign'"
        ).fetchone()["paid_cents"]
    finally:
        conn.close()
    assert paid == 0


# ---------- success & conservation ----------

def test_payment_success_returns_paid_and_outstanding() -> None:
    _make_order("p5", 500)
    resp = _pay("p5", 200, "p5-k1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paid_cents"] == 200 and body["outstanding_cents"] == 300
    assert body["status"] == "accepted"
    # paying the rest flips the order to settled; receivable = paid + outstanding
    rest = _pay("p5", 300, "p5-k2")
    assert rest.status_code == 200
    assert rest.json()["paid_cents"] == 500
    assert rest.json()["outstanding_cents"] == 0
    assert rest.json()["status"] == "settled"


def test_over_payment_conflicts_and_changes_nothing() -> None:
    _make_order("p6", 300)
    assert _pay("p6", 100, "p6-k1").status_code == 200
    resp = _pay("p6", 500, "p6-k2")
    assert resp.status_code == 409
    assert _paid_cents("p6") == 100


def test_distinct_keys_register_independent_payments() -> None:
    _make_order("p7", 1000)
    assert _pay("p7", 100, "p7-k1").status_code == 200
    assert _pay("p7", 250, "p7-k2").status_code == 200
    got = client.get("/orders/p7", headers=H)
    assert got.json()["paid_cents"] == 350
    assert got.json()["outstanding_cents"] == 650


# ---------- idempotent replay ----------

def test_replay_returns_first_result_and_pays_once() -> None:
    _make_order("p8", 500)
    first = _pay("p8", 200, "p8-same-key")
    replay = _pay("p8", 200, "p8-same-key")
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert _paid_cents("p8") == 200  # not 400
    assert _recorded_outcomes("p8") == 1


def test_replay_of_first_error_is_identical() -> None:
    _make_order("p9", 300)
    first = _pay("p9", 500, "p9-same-key")
    assert first.status_code == 409
    replay = _pay("p9", 500, "p9-same-key")
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert _paid_cents("p9") == 0
    # a different key with a valid amount still succeeds afterwards
    assert _pay("p9", 300, "p9-k2").status_code == 200


def test_replay_of_first_not_found_survives_later_order_creation() -> None:
    first = _pay("p10", 100, "p10-same-key")
    assert first.status_code == 404
    _make_order("p10", 300)
    replay = _pay("p10", 100, "p10-same-key")
    assert replay.status_code == 404
    assert _paid_cents("p10") == 0
    # a different fingerprint against the now-existing order succeeds normally
    assert _pay("p10", 100, "p10-k2").status_code == 200


def test_same_key_on_different_orders_is_independent() -> None:
    _make_order("p11a", 500)
    _make_order("p11b", 500)
    a = _pay("p11a", 100, "p11-shared-key")
    b = _pay("p11b", 200, "p11-shared-key")
    assert a.status_code == 200 and a.json()["paid_cents"] == 100
    assert b.status_code == 200 and b.json()["paid_cents"] == 200


def test_replay_does_not_touch_other_documents() -> None:
    _make_order("p12", 1000)
    assert _pay("p12", 400, "p12-k1").status_code == 200
    refund = client.post("/refunds",
                         json={"refund_id": "p12-r1", "order_id": "p12",
                               "amount_cents": 100, "reason": "x"},
                         headers={**H, "Idempotency-Key": "p12-rfk"})
    assert refund.status_code == 201
    # replaying the payment must not move paid_cents nor the refund quota
    assert _pay("p12", 400, "p12-k1").status_code == 200
    assert _paid_cents("p12") == 400
    got = client.get("/refunds/p12-r1", headers=H)
    assert got.json()["status"] == "pending" and got.json()["amount_cents"] == 100


# ---------- concurrency ----------

def test_concurrent_writer_makes_payment_fail_without_side_effects() -> None:
    _make_order("p13", 500)
    conn = connect()
    try:
        # hold the write lock as a concurrent registration would
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE orders SET paid_cents = paid_cents + 50 "
                     "WHERE tenant=? AND order_id=?", (TENANT, "p13"))
        resp = _pay("p13", 100, "p13-k1")
        assert resp.status_code == 409
        assert "concurrent write" in resp.json()["detail"]
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    # nothing changed, nothing recorded; the retry succeeds
    assert _paid_cents("p13") == 0 and _recorded_outcomes("p13") == 0
    ok = _pay("p13", 100, "p13-k1")
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 100


def test_concurrent_distinct_fingerprints_keep_conservation() -> None:
    _make_order("p14", 10_000)
    results: list[tuple[int, str]] = []

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _pay("p14", 100, f"p14-k{i}")
        results.append((resp.status_code, f"p14-k{i}"))

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = [code for code, _ in results]
    assert set(codes) <= {200, 409}
    assert codes.count(200) >= 1
    # every 200 applied its amount exactly once; every 409 changed nothing
    assert _paid_cents("p14") == 100 * codes.count(200)
    assert _recorded_outcomes("p14") == codes.count(200)
    # and each rejected key can be retried into a normal independent payment
    for code, key in results:
        if code == 409:
            assert _pay("p14", 100, key).status_code == 200
    assert _paid_cents("p14") == 600


# ---------- durability across restart ----------

def test_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-payment.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('pt','ro',1000,'CNY')\n"
        "out = orders.register_payment('pt','ro',250,'persist-key')\n"
        "conn = connect()\n"
        "paid = conn.execute(\"SELECT paid_cents FROM orders WHERE tenant='pt' AND order_id='ro'\").fetchone()[0]\n"
        "conn.close()\n"
        "print(json.dumps({'status': out.status, 'paid_body': out.body.get('paid_cents'), 'paid_db': paid}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b  # identical first result on replay
    assert a["status"] == 200
    assert a["paid_body"] == 250
    assert a["paid_db"] == 250  # replayed, not paid twice
