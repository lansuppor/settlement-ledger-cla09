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
TENANT = "rc"
H = {"X-Tenant": TENANT}


def _make_paid_order(order_id: str, amount: int = 1000, paid: int | None = None,
                     tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")
    order_store.add_payment(tenant, order_id, paid if paid is not None else amount)


def _refund(refund_id: str, order_id: str, amount: int, key: str,
            tenant: str = TENANT, reason: str = "customer request"):
    return client.post(
        "/refunds",
        json={"refund_id": refund_id, "order_id": order_id,
              "amount_cents": amount, "reason": reason},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _settle_and_advance(settlement_id: str, order_id: str, amount: int,
                        refund_ids: list[str], tenant: str = TENANT) -> None:
    assert client.post(
        "/settlements",
        json={"settlement_id": settlement_id, "order_id": order_id,
              "amount_cents": amount, "refund_ids": refund_ids, "reason": "周期结算"},
        headers={"X-Tenant": tenant, "Idempotency-Key": f"{settlement_id}-accept"},
    ).status_code == 201
    assert client.post(
        f"/settlements/{settlement_id}/advance",
        headers={"X-Tenant": tenant, "Idempotency-Key": f"{settlement_id}-adv"},
    ).status_code == 200


def _reconcile(batch_id: str, order_id: str, key: str,
               tenant: str = TENANT, note: str = "月度对账"):
    return client.post(
        "/reconciliations",
        json={"batch_id": batch_id, "order_id": order_id, "note": note},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


# ---------- reconcile & read ----------

def test_reconcile_returns_readings_and_balanced_conclusion() -> None:
    _make_paid_order("rco1", 1000)
    _refund("rc1-r1", "rco1", 300, "rc1-k1")                      # pending
    _refund("rc1-r2", "rco1", 200, "rc1-k2")
    _settle_and_advance("rc1-s1", "rco1", 200, ["rc1-r2"])        # effective, settled 200

    resp = _reconcile("rc1-b1", "rco1", "rc1-rec")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"batch_id", "order_id", "note", "paid_cents",
                         "pending_refund_cents", "effective_refund_cents",
                         "effective_settlement_cents", "settled_balance_cents",
                         "conclusion", "status", "created_at", "completed_at"}
    assert body["batch_id"] == "rc1-b1" and body["order_id"] == "rco1"
    assert body["note"] == "月度对账"
    assert body["paid_cents"] == 1000
    assert body["pending_refund_cents"] == 300
    assert body["effective_refund_cents"] == 200
    assert body["effective_settlement_cents"] == 200
    # settled balance = paid - (pending + effective) = 1000 - 500
    assert body["settled_balance_cents"] == 500
    assert body["paid_cents"] == (body["pending_refund_cents"]
                                  + body["effective_refund_cents"]
                                  + body["settled_balance_cents"])
    assert body["conclusion"] == "balanced"
    assert body["status"] == "completed"
    assert body["completed_at"] >= body["created_at"]

    got = client.get("/reconciliations/rc1-b1", headers=H)
    assert got.status_code == 200 and got.json() == body


def test_reconcile_partially_paid_order_without_refunds() -> None:
    _make_paid_order("rco2", 1000, paid=400)
    body = _reconcile("rc2-b1", "rco2", "rc2-rec").json()
    assert body["paid_cents"] == 400
    assert body["pending_refund_cents"] == 0
    assert body["effective_refund_cents"] == 0
    assert body["effective_settlement_cents"] == 0
    assert body["settled_balance_cents"] == 400
    assert body["conclusion"] == "balanced"


def test_reconcile_does_not_change_order_refund_or_settlement() -> None:
    _make_paid_order("rco3", 1000, paid=800)
    _refund("rc3-r1", "rco3", 100, "rc3-k1")
    _settle_and_advance("rc3-s1", "rco3", 100, ["rc3-r1"])
    before_order = client.get("/orders/rco3", headers=H).json()
    before_refund = client.get("/refunds/rc3-r1", headers=H).json()
    before_settlement = client.get("/settlements/rc3-s1", headers=H).json()

    assert _reconcile("rc3-b1", "rco3", "rc3-rec").status_code == 201

    assert client.get("/orders/rco3", headers=H).json() == before_order
    assert client.get("/refunds/rc3-r1", headers=H).json() == before_refund
    assert client.get("/settlements/rc3-s1", headers=H).json() == before_settlement


def test_read_reconciliation_is_tenant_isolated() -> None:
    _make_paid_order("rco4", 500)
    _reconcile("rc4-b1", "rco4", "rc4-rec")
    assert client.get("/reconciliations/rc4-b1", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/reconciliations/rc4-b1").status_code == 400
    assert _reconcile("rc4-b2", "rco4", "rc4-rec-x", tenant="t2").status_code == 404


def test_reconcile_missing_or_cross_tenant_order_is_not_found() -> None:
    resp = _reconcile("rc5-b1", "no-such-order", "rc5-rec")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "reconciliation target not found"

    _make_paid_order("foreign-o5", 500, tenant="t2")
    assert _reconcile("rc5-b2", "foreign-o5", "rc5-rec-2", tenant="t1").status_code == 404
    # no object existence leaked: neither batch was created
    assert client.get("/reconciliations/rc5-b1", headers=H).status_code == 404
    assert client.get("/reconciliations/rc5-b2", headers={"X-Tenant": "t1"}).status_code == 404


def test_missing_headers_are_rejected() -> None:
    _make_paid_order("rco6", 500)
    no_tenant = client.post("/reconciliations",
                            json={"batch_id": "rc6-b1", "order_id": "rco6"},
                            headers={"Idempotency-Key": "rc6-rec"})
    assert no_tenant.status_code == 400
    no_key = client.post("/reconciliations",
                         json={"batch_id": "rc6-b1", "order_id": "rco6"}, headers=H)
    assert no_key.status_code == 400
    missing_fields = client.post("/reconciliations", json={"batch_id": "rc6-b1"},
                                 headers={**H, "Idempotency-Key": "rc6-rec"})
    assert missing_fields.status_code == 422
    assert client.get("/reconciliations/rc6-b1", headers=H).status_code == 404


def test_duplicate_batch_id_conflicts_and_keeps_first() -> None:
    _make_paid_order("rco7", 500)
    first = _reconcile("rc7-b1", "rco7", "rc7-rec-a")
    assert first.status_code == 201
    dup = _reconcile("rc7-b1", "rco7", "rc7-rec-b")  # different request, same batch id
    assert dup.status_code == 409
    assert dup.json()["detail"] == "reconciliation batch already exists"
    assert client.get("/reconciliations/rc7-b1", headers=H).json() == first.json()


# ---------- idempotent replay ----------

def test_replay_returns_first_result_and_records_once() -> None:
    _make_paid_order("rco8", 500, paid=200)
    a = _reconcile("rc8-b1", "rco8", "same-rec-key")
    assert a.json()["paid_cents"] == 200
    # later order activity must not alter the replayed first result
    order_store.add_payment(TENANT, "rco8", 300)
    b = _reconcile("rc8-b1", "rco8", "same-rec-key")
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='rc8-b1' AND operation='reconcile'").fetchone()["c"]
        batches = conn.execute(
            "SELECT COUNT(*) c FROM reconciliations WHERE batch_id='rc8-b1'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1 and batches == 1


def test_failed_reconcile_is_replayed_identically() -> None:
    first = _reconcile("rc9-b1", "rc9-ghost", "rc9-rec")
    assert first.status_code == 404
    # even after the order later appears, the same fingerprint replays the old 404
    _make_paid_order("rc9-ghost", 500)
    replay = _reconcile("rc9-b1", "rc9-ghost", "rc9-rec")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert client.get("/reconciliations/rc9-b1", headers=H).status_code == 404
    # a different fingerprint re-validates against current state and now succeeds
    assert _reconcile("rc9-b1", "rc9-ghost", "rc9-rec-retry").status_code == 201


def test_completed_batch_is_not_retroactively_modified() -> None:
    _make_paid_order("rco10", 1000, paid=600)
    first = _reconcile("rc10-b1", "rco10", "rc10-rec").json()
    assert first["paid_cents"] == 600 and first["settled_balance_cents"] == 600

    # order keeps moving: more payment and a new pending refund
    order_store.add_payment(TENANT, "rco10", 400)
    _refund("rc10-r1", "rco10", 250, "rc10-k1")

    got = client.get("/reconciliations/rc10-b1", headers=H)
    assert got.status_code == 200 and got.json() == first

    # a fresh batch reflects the new readings
    second = _reconcile("rc10-b2", "rco10", "rc10-rec-2").json()
    assert second["paid_cents"] == 1000
    assert second["pending_refund_cents"] == 250
    assert second["settled_balance_cents"] == 750
    assert second["conclusion"] == "balanced"


# ---------- concurrency ----------

def test_in_progress_batch_blocks_new_batch_for_same_order() -> None:
    _make_paid_order("rco11", 500)
    conn = connect()
    try:
        # simulate an in-progress batch owned by another worker
        conn.execute(
            "INSERT INTO reconciliations(tenant, batch_id, order_id, note, status, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (TENANT, "rc11-b0", "rco11", "", "in_progress", "2026-01-01T00:00:00+00:00"),
        )
        resp = _reconcile("rc11-b1", "rco11", "rc11-rec")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "order already has an in-progress reconciliation batch"
        # the existing batch is untouched and no new batch appeared
        row = conn.execute(
            "SELECT status, completed_at FROM reconciliations WHERE batch_id='rc11-b0'").fetchone()
        assert row["status"] == "in_progress" and row["completed_at"] is None
        assert client.get("/reconciliations/rc11-b1", headers=H).status_code == 404
        # a different order is unaffected
        _make_paid_order("rco11b", 500)
        assert _reconcile("rc11-b2", "rco11b", "rc11-rec-2").status_code == 201
    finally:
        conn.execute("DELETE FROM reconciliations WHERE batch_id='rc11-b0'")
        conn.close()


def test_concurrent_reconciles_same_order_single_winner() -> None:
    _make_paid_order("rco12", 1000)
    results: list[tuple[int, str]] = []

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _reconcile(f"rc12-b{i}", "rco12", f"rc12-rec-{i}")
        results.append((resp.status_code, f"rc12-b{i}"))

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(code for code, _ in results)
    assert set(codes) <= {201, 409}
    assert codes.count(201) >= 1
    # every 201 corresponds to a persisted completed batch; every 409 left nothing
    conn = connect()
    try:
        for code, batch_id in results:
            row = conn.execute(
                "SELECT status FROM reconciliations WHERE batch_id=?", (batch_id,)).fetchone()
            if code == 201:
                assert row is not None and row["status"] == "completed"
            else:
                assert row is None
        in_progress = conn.execute(
            "SELECT COUNT(*) c FROM reconciliations WHERE status='in_progress'").fetchone()["c"]
    finally:
        conn.close()
    assert in_progress == 0


def test_concurrent_writer_makes_reconcile_fail_as_a_whole() -> None:
    _make_paid_order("rco13", 1000, paid=500)
    conn = connect()
    try:
        # hold the write lock as a concurrent payment would
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE orders SET paid_cents = paid_cents + 100 "
                     "WHERE tenant=? AND order_id=?", (TENANT, "rco13"))
        resp = _reconcile("rc13-b1", "rco13", "rc13-rec")
        assert resp.status_code == 409
        assert "concurrent write" in resp.json()["detail"]
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    # nothing left behind: no batch, no recorded outcome
    assert client.get("/reconciliations/rc13-b1", headers=H).status_code == 404
    # and the retry against the settled state succeeds
    ok = _reconcile("rc13-b1", "rco13", "rc13-rec")
    assert ok.status_code == 201 and ok.json()["paid_cents"] == 500


# ---------- conservation ----------

def test_conservation_holds_across_reconciled_lifecycle() -> None:
    _make_paid_order("rco14", 1000)
    _refund("rc14-r1", "rco14", 300, "rc14-k1")
    _refund("rc14-r2", "rco14", 200, "rc14-k2")
    _settle_and_advance("rc14-s1", "rco14", 200, ["rc14-r2"])
    assert _reconcile("rc14-b1", "rco14", "rc14-rec").status_code == 201
    client.post("/refunds/rc14-r1/reverse", headers={**H, "Idempotency-Key": "rc14-rev"})
    _refund("rc14-r3", "rco14", 300, "rc14-k3")
    assert _reconcile("rc14-b2", "rco14", "rc14-rec-2").status_code == 201

    conn = connect()
    try:
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE order_id='rco14'").fetchone()
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
            "WHERE order_id='rco14' AND status IN ('pending','effective')").fetchone()["s"]
    finally:
        conn.close()
    refundable = order["paid_cents"] - occupied
    assert occupied == 500  # r2 effective 200 + r3 pending 300
    assert refundable == 500
    assert occupied <= order["paid_cents"] <= order["amount_cents"]
    assert _refund("rc14-r4", "rco14", 501, "rc14-k4").status_code == 409
    assert _refund("rc14-r5", "rco14", 500, "rc14-k5").status_code == 201


# ---------- durability across restart ----------

def test_reconcile_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-reconcile.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders, refunds, reconciliations\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('rc','ro',1000,'CNY')\n"
        "    orders.add_payment('rc','ro',700)\n"
        "    refunds.accept('rc','rr','ro',200,'why','r-key')\n"
        "out = reconciliations.execute('rc','rb','ro','月度对账','rec-key')\n"
        "print(json.dumps({'http': out.status, 'body': out.body}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b
    assert a["http"] == 201
    assert a["body"]["paid_cents"] == 700
    assert a["body"]["pending_refund_cents"] == 200
    assert a["body"]["settled_balance_cents"] == 500
    assert a["body"]["conclusion"] == "balanced"
    assert a["body"]["status"] == "completed"
