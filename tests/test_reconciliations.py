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
    if paid is None or paid > 0:
        order_store.add_payment(tenant, order_id, paid if paid is not None else amount)


def _pay(order_id: str, amount: int, tenant: str = TENANT):
    return client.post(f"/orders/{order_id}/payments",
                       json={"amount_cents": amount}, headers={"X-Tenant": tenant})


def _refund(refund_id: str, order_id: str, amount: int, key: str,
            tenant: str = TENANT, reason: str = "customer request"):
    return client.post(
        "/refunds",
        json={"refund_id": refund_id, "order_id": order_id,
              "amount_cents": amount, "reason": reason},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _settle(settlement_id: str, order_id: str, amount: int, refund_ids: list[str],
            key: str, reason: str = "周期结算", tenant: str = TENANT):
    return client.post(
        "/settlements",
        json={"settlement_id": settlement_id, "order_id": order_id,
              "amount_cents": amount, "refund_ids": refund_ids, "reason": reason},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _advance(settlement_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/settlements/{settlement_id}/advance",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _revoke(settlement_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/settlements/{settlement_id}/revoke",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _reconcile(batch_id: str, order_id: str, key: str, note: str = "月末对账",
               tenant: str = TENANT):
    return client.post(
        "/reconciliations",
        json={"batch_id": batch_id, "order_id": order_id, "note": note},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _refund_status(refund_id: str, tenant: str = TENANT) -> str:
    return client.get(f"/refunds/{refund_id}", headers={"X-Tenant": tenant}).json()["status"]


def _in_progress_count(tenant: str = TENANT, order_id: str | None = None) -> int:
    conn = connect()
    try:
        sql = "SELECT COUNT(*) c FROM reconciliation_batches WHERE tenant=? AND status='in_progress'"
        params: tuple = (tenant,)
        if order_id is not None:
            sql += " AND order_id=?"
            params = (tenant, order_id)
        return conn.execute(sql, params).fetchone()["c"]
    finally:
        conn.close()


# ---------- execute & read ----------

def test_reconcile_snapshots_totals_and_completes() -> None:
    _make_paid_order("co1", 1000)
    _refund("c1-r1", "co1", 300, "c1-k1")
    _refund("c1-r2", "co1", 200, "c1-k2")
    _settle("c1-s", "co1", 200, ["c1-r2"], "c1-s-accept")
    assert _advance("c1-s", "c1-s-adv").status_code == 200

    resp = _reconcile("c1-b", "co1", "c1-b-key")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {
        "batch_id", "order_id", "note", "status",
        "paid_cents", "pending_refunds_cents", "effective_refunds_cents",
        "effective_settlements_cents", "verified_balance_cents", "conclusion",
        "created_at", "completed_at",
    }
    assert body["batch_id"] == "c1-b" and body["order_id"] == "co1"
    assert body["status"] == "completed"
    assert body["paid_cents"] == 1000
    assert body["pending_refunds_cents"] == 300
    assert body["effective_refunds_cents"] == 200
    assert body["effective_settlements_cents"] == 200
    assert body["verified_balance_cents"] == 500
    assert body["conclusion"] == "balanced"
    assert body["completed_at"] >= body["created_at"]

    got = client.get("/reconciliations/c1-b", headers=H)
    assert got.status_code == 200 and got.json() == body
    # the check closes itself: no in-progress batch is ever observable
    assert _in_progress_count(order_id="co1") == 0


def test_reconcile_zero_movement_order_is_balanced() -> None:
    order_store.insert(TENANT, "co1z", 500, "CNY")  # accepted, no payment
    resp = _reconcile("c1z-b", "co1z", "c1z-key")
    assert resp.status_code == 201
    body = resp.json()
    assert body["paid_cents"] == 0
    assert body["pending_refunds_cents"] == body["effective_refunds_cents"] == 0
    assert body["effective_settlements_cents"] == 0
    assert body["verified_balance_cents"] == 0
    assert body["conclusion"] == "balanced"


def test_reconcile_tracks_revoked_settlement_refunds_back_to_pending() -> None:
    _make_paid_order("co1r", 1000)
    _refund("c1r-r1", "co1r", 200, "c1r-k1")
    _settle("c1r-s", "co1r", 200, ["c1r-r1"], "c1r-s-accept")
    _advance("c1r-s", "c1r-s-adv")
    assert _revoke("c1r-s", "c1r-s-rev").status_code == 200

    body = _reconcile("c1r-b", "co1r", "c1r-b-key").json()
    assert body["pending_refunds_cents"] == 200
    assert body["effective_refunds_cents"] == 0
    assert body["effective_settlements_cents"] == 0
    assert body["verified_balance_cents"] == 800
    assert body["conclusion"] == "balanced"


# ---------- read isolation ----------

def test_read_batch_is_tenant_isolated() -> None:
    _make_paid_order("co2", 500)
    assert _reconcile("c2-b", "co2", "c2-key").status_code == 201
    assert client.get("/reconciliations/c2-b", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/reconciliations/c2-b").status_code == 400


def test_reconcile_missing_or_cross_tenant_order_is_not_found() -> None:
    resp = _reconcile("c3-b", "no-such-order", "c3-key")
    assert resp.status_code == 404 and resp.json()["detail"] == "reconciliation target order not found"

    _make_paid_order("c-foreign-o", 500, tenant="t2")
    cross = _reconcile("c3-b2", "c-foreign-o", "c3-key2", tenant="t1")
    assert cross.status_code == 404
    # nothing created, no existence leaked
    assert client.get("/reconciliations/c3-b", headers=H).status_code == 404
    assert client.get("/reconciliations/c3-b2", headers={"X-Tenant": "t1"}).status_code == 404


# ---------- conflicts ----------

def test_duplicate_batch_identifier_conflicts_and_keeps_first() -> None:
    _make_paid_order("co4", 500)
    first = _reconcile("c4-b", "co4", "c4-key-a")
    assert first.status_code == 201
    dup = _reconcile("c4-b", "co4", "c4-key-b")
    assert dup.status_code == 409 and dup.json()["detail"] == "reconciliation batch already exists"
    # the first conclusion is untouched and still readable
    assert client.get("/reconciliations/c4-b", headers=H).json() == first.json()


def test_a_new_batch_is_allowed_after_completion() -> None:
    _make_paid_order("co4b", 500)
    assert _reconcile("c4b-a", "co4b", "c4b-key-a").status_code == 201
    # prior batch is completed, not in progress: another run is accepted
    second = _reconcile("c4b-b", "co4b", "c4b-key-b")
    assert second.status_code == 201 and second.json()["batch_id"] == "c4b-b"


def test_concurrent_initiations_only_one_enters_progress() -> None:
    _make_paid_order("co5", 1000)
    results: list[tuple[str, str, int]] = []

    def run(batch_id: str, key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _reconcile(batch_id, "co5", key)
        results.append((batch_id, key, resp.status_code))

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(f"c5-b{i}", f"c5-key{i}", barrier))
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(code for _, _, code in results)
    assert statuses.count(201) == 1 and statuses.count(409) == 5
    losers = [(b, k) for b, k, code in results if code == 409]
    # every loser reported the distinguishable in-progress conflict ...
    for b, k in losers:
        again = _reconcile(b, "co5", k)
        assert again.status_code == 409
        assert again.json()["detail"] == "another reconciliation for this order is already in progress"
    # ... created no batch of their own ...
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM reconciliation_batches WHERE tenant=? AND order_id='co5'",
            (TENANT,),
        ).fetchone()["c"]
        idem = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE tenant=? AND operation='reconcile' AND target_id LIKE 'c5-b%'",
            (TENANT,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 1 and idem == 6
    assert _in_progress_count(order_id="co5") == 0
    # ... once the winner is done, a fresh request on the order runs normally
    assert _reconcile("c5-after", "co5", "c5-key-after").status_code == 201


# ---------- idempotent replay ----------

def test_success_replay_returns_identical_result_and_runs_once() -> None:
    _make_paid_order("co6", 800)
    _refund("c6-r1", "co6", 300, "c6-k1")
    a = _reconcile("c6-b", "co6", "same-key")
    b = _reconcile("c6-b", "co6", "same-key")
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json()
    conn = connect()
    try:
        batches = conn.execute(
            "SELECT COUNT(*) c, MIN(created_at) ca, MIN(completed_at) ct "
            "FROM reconciliation_batches WHERE tenant=? AND batch_id='c6-b'",
            (TENANT,),
        ).fetchone()
        idem = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE tenant=? AND operation='reconcile' AND target_id='c6-b'",
            (TENANT,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert batches["c"] == 1 and idem == 1
    assert a.json()["created_at"] == batches["ca"]
    assert a.json()["completed_at"] == batches["ct"]


def test_replayed_404_stays_404_after_order_appears() -> None:
    first = _reconcile("c7-b", "co7-late", "c7-key")
    assert first.status_code == 404
    _make_paid_order("co7-late", 500)
    replay = _reconcile("c7-b", "co7-late", "c7-key")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert client.get("/reconciliations/c7-b", headers=H).status_code == 404
    # a different fingerprint evaluates current state and succeeds
    assert _reconcile("c7-b2", "co7-late", "c7-key-retry").status_code == 201


def test_replay_survives_process_restart() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "restart-reconcile.sqlite")
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders, reconciliations\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('rc','rro',1000,'CNY')\n"
        "    orders.add_payment('rc','rro',700)\n"
        "out = reconciliations.execute('rc','rrb','rro','月末对账','rr-key')\n"
        "conn = connect()\n"
        "n = conn.execute(\"SELECT COUNT(*) FROM reconciliation_batches WHERE batch_id='rrb'\").fetchone()[0]\n"
        "idem = conn.execute(\"SELECT COUNT(*) FROM idempotent_requests WHERE target_id='rrb'\").fetchone()[0]\n"
        "conn.close()\n"
        "print(json.dumps({'http': out.status, 'body': out.body, 'rows': n, 'idem': idem}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b
    assert a["http"] == 201 and a["rows"] == 1 and a["idem"] == 1
    assert a["body"]["status"] == "completed"
    assert a["body"]["paid_cents"] == 700 and a["body"]["verified_balance_cents"] == 700


# ---------- read-only, non-retroactive, atomic ----------

def test_reconcile_does_not_mutate_orders_refunds_or_settlements() -> None:
    _make_paid_order("co8", 1000)
    _refund("c8-r1", "co8", 300, "c8-k1")
    _refund("c8-r2", "co8", 200, "c8-k2")
    _settle("c8-s", "co8", 200, ["c8-r2"], "c8-s-accept")
    _advance("c8-s", "c8-s-adv")

    before_order = client.get("/orders/co8", headers=H).json()
    assert _reconcile("c8-b", "co8", "c8-key").status_code == 201
    after_order = client.get("/orders/co8", headers=H).json()
    assert before_order == after_order
    assert _refund_status("c8-r1") == "pending"
    assert _refund_status("c8-r2") == "effective"
    assert client.get("/settlements/c8-s", headers=H).json()["status"] == "effective"
    # refundable quota conservation still holds
    conn = connect()
    try:
        row = conn.execute(
            "SELECT o.paid_cents p, o.amount_cents a, "
            "COALESCE(SUM(CASE WHEN r.status IN ('pending','effective') THEN r.amount_cents ELSE 0 END),0) occ "
            "FROM orders o LEFT JOIN refunds r ON r.tenant=o.tenant AND r.order_id=o.order_id "
            "WHERE o.tenant=? AND o.order_id='co8'",
            (TENANT,),
        ).fetchone()
    finally:
        conn.close()
    assert row["p"] - row["occ"] == 500
    assert row["occ"] <= row["p"] <= row["a"]


def test_completed_conclusion_is_not_retroactively_changed() -> None:
    _make_paid_order("co9", 1000, paid=400)
    first = _reconcile("c9-b1", "co9", "c9-key1").json()
    assert first["paid_cents"] == 400 and first["verified_balance_cents"] == 400

    assert _pay("co9", 600).status_code == 200
    assert _refund("c9-r1", "co9", 250, "c9-k-r1").status_code == 201

    # the finished batch keeps its original snapshot ...
    got = client.get("/reconciliations/c9-b1", headers=H).json()
    assert got == first
    # ... a fresh batch reconciles the current state
    second = _reconcile("c9-b2", "co9", "c9-key2").json()
    assert second["paid_cents"] == 1000
    assert second["pending_refunds_cents"] == 250
    assert second["verified_balance_cents"] == 750


def test_concurrent_payment_and_reconciliation_leaves_no_progress_and_conserves() -> None:
    _make_paid_order("co10", 1000, paid=500)
    outcomes: list[str] = []

    def run(kind: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        if kind == "pay":
            resp = _pay("co10", 500)
            outcomes.append(f"pay:{resp.status_code}")
        else:
            resp = _reconcile("c10-b", "co10", "c10-key")
            outcomes.append(f"rec:{resp.status_code}")

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=run, args=("pay", barrier))
    t2 = threading.Thread(target=run, args=("rec", barrier))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert "pay:200" in outcomes and "rec:201" in outcomes
    assert _in_progress_count(order_id="co10") == 0
    batch = client.get("/reconciliations/c10-b", headers=H).json()
    # the snapshot is either entirely pre-payment or entirely post-payment,
    # and self-consistent either way (no refunds on this order)
    assert batch["paid_cents"] in (500, 1000)
    assert batch["verified_balance_cents"] == batch["paid_cents"]
    assert client.get("/orders/co10", headers=H).json()["paid_cents"] == 1000


# ---------- request validation ----------

def test_invalid_requests() -> None:
    _make_paid_order("co11", 500)
    missing_field = client.post(
        "/reconciliations", json={"order_id": "co11"},
        headers={**H, "Idempotency-Key": "c11-bad1"},
    )
    assert missing_field.status_code == 422
    empty_id = client.post(
        "/reconciliations", json={"batch_id": "", "order_id": "co11"},
        headers={**H, "Idempotency-Key": "c11-bad2"},
    )
    assert empty_id.status_code == 422
    no_tenant = client.post(
        "/reconciliations", json={"batch_id": "c11-x", "order_id": "co11"},
        headers={"Idempotency-Key": "c11-bad3"},
    )
    assert no_tenant.status_code == 400
    no_key = client.post(
        "/reconciliations", json={"batch_id": "c11-y", "order_id": "co11"}, headers=H,
    )
    assert no_key.status_code == 400
    # rejected requests leave no batch behind
    for bid in ("c11-bad1", "c11-bad2", "c11-bad3", "c11-x", "c11-y"):
        assert client.get(f"/reconciliations/{bid}", headers=H).status_code == 404
