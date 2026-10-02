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
TENANT = "pay"
H = {"X-Tenant": TENANT}


def _make_order(order_id: str, amount: int = 1000, tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")


def _pay(order_id: str, amount: int, key: str | None, tenant: str = TENANT):
    headers = {"X-Tenant": tenant}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(f"/orders/{order_id}/payments",
                       json={"amount_cents": amount}, headers=headers)


def _paid_cents(order_id: str, tenant: str = TENANT) -> int:
    return client.get(f"/orders/{order_id}", headers={"X-Tenant": tenant}).json()["paid_cents"]


# ---------- registration & conservation ----------

def test_payment_returns_paid_and_outstanding() -> None:
    _make_order("po1", 500)
    resp = _pay("po1", 200, "po1-k1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paid_cents"] == 200 and body["outstanding_cents"] == 300
    # 应收 = 已收 + 未收
    assert body["amount_cents"] == body["paid_cents"] + body["outstanding_cents"]


def test_payment_fully_settles_order() -> None:
    _make_order("po2", 300)
    assert _pay("po2", 300, "po2-k1").status_code == 200
    got = client.get("/orders/po2", headers=H).json()
    assert got["paid_cents"] == 300 and got["outstanding_cents"] == 0
    assert got["status"] == "settled"


def test_payment_exceeding_outstanding_conflicts_without_change() -> None:
    _make_order("po3", 300)
    assert _pay("po3", 200, "po3-k1").status_code == 200
    resp = _pay("po3", 200, "po3-k2")
    assert resp.status_code == 409
    assert _paid_cents("po3") == 200


def test_missing_or_cross_tenant_order_is_not_found() -> None:
    assert _pay("po4-ghost", 100, "po4-k1").status_code == 404
    _make_order("po4-foreign", 300, tenant="other")
    assert _pay("po4-foreign", 100, "po4-k2", tenant=TENANT).status_code == 404
    # nothing registered on the foreign order
    assert _paid_cents("po4-foreign", tenant="other") == 0


def test_invalid_params_are_rejected_without_registration() -> None:
    _make_order("po5", 300)
    # missing tenant header
    assert client.post("/orders/po5/payments", json={"amount_cents": 100},
                       headers={"Idempotency-Key": "po5-k1"}).status_code == 400
    # missing idempotency key
    assert _pay("po5", 100, None).status_code == 400
    # non-positive / non-integer amount
    assert _pay("po5", 0, "po5-k2").status_code == 422
    assert _pay("po5", -50, "po5-k3").status_code == 422
    assert client.post("/orders/po5/payments", json={"amount_cents": "abc"},
                       headers={**H, "Idempotency-Key": "po5-k4"}).status_code == 422
    assert _paid_cents("po5") == 0


# ---------- idempotent replay ----------

def test_same_fingerprint_replays_without_second_registration() -> None:
    _make_order("po6", 500)
    first = _pay("po6", 200, "po6-key")
    replay = _pay("po6", 200, "po6-key")
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    # paid_cents accumulated exactly once
    assert _paid_cents("po6") == 200
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE operation='order_payment' AND target_id='po6'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_failed_result_is_replayed_identically() -> None:
    # a first request that exceeds the outstanding amount is remembered as 409 ...
    _make_order("po7", 300)
    first = _pay("po7", 400, "po7-key")
    assert first.status_code == 409
    replay = _pay("po7", 400, "po7-key")
    assert replay.status_code == 409 and replay.json() == first.json()
    assert _paid_cents("po7") == 0

    # ... and a first 404 replays as 404 even after the order later exists
    missing = _pay("po7-ghost", 100, "po7-ghost-key")
    assert missing.status_code == 404
    _make_order("po7-ghost", 300)
    assert _pay("po7-ghost", 100, "po7-ghost-key").status_code == 404
    assert _paid_cents("po7-ghost") == 0
    # a different fingerprint against the now-existing order succeeds normally
    assert _pay("po7-ghost", 100, "po7-ghost-key-2").status_code == 200


def test_distinct_fingerprints_register_independent_payments() -> None:
    _make_order("po8", 500)
    assert _pay("po8", 200, "po8-k1").status_code == 200
    resp = _pay("po8", 150, "po8-k2")
    assert resp.status_code == 200
    assert resp.json()["paid_cents"] == 350 and resp.json()["outstanding_cents"] == 150


def test_same_key_on_different_orders_replays_per_order() -> None:
    _make_order("po9-a", 500)
    _make_order("po9-b", 500)
    first_a = _pay("po9-a", 100, "po9-shared-key")
    first_b = _pay("po9-b", 200, "po9-shared-key")
    assert first_a.status_code == first_b.status_code == 200
    assert first_a.json()["paid_cents"] == 100 and first_b.json()["paid_cents"] == 200
    # each order replays its own first result under the shared key
    assert _pay("po9-a", 100, "po9-shared-key").json() == first_a.json()
    assert _pay("po9-b", 200, "po9-shared-key").json() == first_b.json()
    assert _paid_cents("po9-a") == 100 and _paid_cents("po9-b") == 200


def test_concurrent_distinct_fingerprints_single_winner_per_attempt() -> None:
    _make_order("po10", 10_000)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        results.append(_pay("po10", 700, f"po10-key-{i}").status_code)

    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) <= {200, 409}
    assert results.count(200) >= 1
    # conservation: paid_cents equals exactly the sum of the winning payments
    assert _paid_cents("po10") == 700 * results.count(200)


def test_concurrent_writer_conflict_changes_nothing_and_retry_succeeds() -> None:
    _make_order("po11", 500)
    conn = connect()
    try:
        # hold the write lock as a concurrent writer would
        conn.execute("BEGIN IMMEDIATE")
        resp = _pay("po11", 100, "po11-key")
        assert resp.status_code == 409
        assert "concurrent write" in resp.json()["detail"]
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    # the conflict left nothing behind: no payment, no recorded outcome
    assert _paid_cents("po11") == 0
    ok = _pay("po11", 100, "po11-key")
    assert ok.status_code == 200 and ok.json()["paid_cents"] == 100


# ---------- durability across restart ----------

def test_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-payment.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import json, sys\n"
        "from app.store.db import migrate\n"
        "from app.store import orders\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('pay','pro',500,'CNY')\n"
        "out = orders.register_payment('pay','pro',200,'restart-key')\n"
        "after = orders.get('pay','pro')\n"
        "print(json.dumps({'http': out.status, 'body': out.body, 'paid': after['paid_cents']}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a["http"] == b["http"] == 200
    assert a["body"] == b["body"]
    # the replay did not register a second payment
    assert a["paid"] == b["paid"] == 200


# ---------- isolation from the rest of the ledger ----------

def test_payment_only_changes_the_order_itself() -> None:
    _make_order("po12", 1000)
    assert _pay("po12", 600, "po12-k1").status_code == 200
    # refundable = paid - (pending + effective) refunds still holds after payment
    client.post("/refunds",
                json={"refund_id": "po12-r1", "order_id": "po12",
                      "amount_cents": 400, "reason": "x"},
                headers={**H, "Idempotency-Key": "po12-r1"})
    resp = client.post("/refunds",
                       json={"refund_id": "po12-r2", "order_id": "po12",
                             "amount_cents": 300, "reason": "x"},
                       headers={**H, "Idempotency-Key": "po12-r2"})
    assert resp.status_code == 409  # 400 pending refund occupies the refundable amount
    got = client.get("/orders/po12", headers=H).json()
    assert got["paid_cents"] == 600 and got["outstanding_cents"] == 400
