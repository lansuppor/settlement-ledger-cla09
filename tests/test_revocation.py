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
TENANT = "rv"
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


def _refund_status(refund_id: str, tenant: str = TENANT) -> str:
    return client.get(f"/refunds/{refund_id}", headers={"X-Tenant": tenant}).json()["status"]


def _claim_count(refund_id: str) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM settlement_claims WHERE tenant=? AND refund_id=?",
            (TENANT, refund_id),
        ).fetchone()["c"]
    finally:
        conn.close()


def _advanced(settlement_id: str, refund_ids: list[str], amount: int,
              order_id: str) -> None:
    assert _settle(settlement_id, order_id, amount, refund_ids, f"{settlement_id}-accept").status_code == 201
    assert _advance(settlement_id, f"{settlement_id}-adv").status_code == 200


# ---------- basic revoke ----------

def test_revoke_releases_claims_and_returns_refunds_to_pending() -> None:
    _make_paid_order("o1", 1000)
    _refund("v1-r1", "o1", 300, "v1-k1")
    _refund("v1-r2", "o1", 200, "v1-k2")
    _advanced("v1-s", ["v1-r1", "v1-r2"], 500, "o1")
    assert _refund_status("v1-r1") == "effective"
    assert _claim_count("v1-r1") == 1

    resp = _revoke("v1-s", "v1-rev")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"settlement_id", "order_id", "amount_cents", "refund_ids",
                         "reason", "status", "created_at", "updated_at"}
    assert body["status"] == "revoked"
    assert body["refund_ids"] == ["v1-r1", "v1-r2"]

    got = client.get("/settlements/v1-s", headers=H).json()
    assert got["status"] == "revoked" and got == body
    # claims lifted, refunds are pending again and keep occupying quota
    assert _refund_status("v1-r1") == "pending"
    assert _refund_status("v1-r2") == "pending"
    assert _claim_count("v1-r1") == 0 and _claim_count("v1-r2") == 0


def test_revoked_refunds_can_be_settled_by_another_settlement() -> None:
    _make_paid_order("o2", 1000)
    _refund("v2-r1", "o2", 400, "v2-k1")
    _advanced("v2-sa", ["v2-r1"], 400, "o2")
    # a second pending settlement referencing the same refund is accepted while s a is effective
    assert _settle("v2-sb", "o2", 400, ["v2-r1"], "v2-sb-accept").status_code == 201
    assert _advance("v2-sb", "v2-sb-adv-before").status_code == 409

    assert _revoke("v2-sa", "v2-rev").status_code == 200
    # the old settlement is terminal and cannot advance again ...
    assert _advance("v2-sa", "v2-sa-adv-again").status_code == 409
    # ... while the other pending settlement now claims the freed refund
    assert _advance("v2-sb", "v2-sb-adv").status_code == 200
    assert _refund_status("v2-r1") == "effective"
    assert _claim_count("v2-r1") == 1
    conn = connect()
    try:
        owner = conn.execute(
            "SELECT settlement_id FROM settlement_claims WHERE tenant=? AND refund_id='v2-r1'",
            (TENANT,),
        ).fetchone()["settlement_id"]
    finally:
        conn.close()
    assert owner == "v2-sb"


def test_revoke_pending_settlement_conflicts_without_change() -> None:
    _make_paid_order("o3", 500)
    _refund("v3-r1", "o3", 100, "v3-k1")
    _settle("v3-s", "o3", 100, ["v3-r1"], "v3-accept")
    resp = _revoke("v3-s", "v3-rev")
    assert resp.status_code == 409
    assert client.get("/settlements/v3-s", headers=H).json()["status"] == "pending"
    assert _refund_status("v3-r1") == "pending"
    assert _claim_count("v3-r1") == 0
    # the pending settlement can still be advanced afterwards
    assert _advance("v3-s", "v3-adv").status_code == 200


def test_revoke_already_revoked_conflicts_and_keeps_state() -> None:
    _make_paid_order("o4", 500)
    _refund("v4-r1", "o4", 500, "v4-k1")
    _advanced("v4-s", ["v4-r1"], 500, "o4")
    assert _revoke("v4-s", "v4-rev-a").status_code == 200
    again = _revoke("v4-s", "v4-rev-b")
    assert again.status_code == 409 and again.json()["detail"] == "settlement already revoked"
    assert client.get("/settlements/v4-s", headers=H).json()["status"] == "revoked"
    assert _refund_status("v4-r1") == "pending"
    assert _claim_count("v4-r1") == 0


def test_revoke_missing_or_cross_tenant_is_not_found() -> None:
    assert _revoke("ghost-settlement", "ghost-rev").status_code == 404

    _make_paid_order("o5", 500)
    _refund("v5-r1", "o5", 100, "v5-k1")
    _advanced("v5-s", ["v5-r1"], 100, "o5")
    assert _revoke("v5-s", "v5-rev-t2", tenant="t2").status_code == 404
    # no existence leaked and no state changed across tenants
    assert client.get("/settlements/v5-s", headers=H).json()["status"] == "effective"
    assert _refund_status("v5-r1") == "effective"
    assert _claim_count("v5-r1") == 1


def test_revoke_requires_headers() -> None:
    assert client.post("/settlements/v1-s/revoke", headers=H).status_code == 400
    assert client.post("/settlements/v1-s/revoke",
                       headers={"Idempotency-Key": "k"}).status_code == 400


# ---------- interaction with refund reversal ----------

def test_revoke_keeps_refunds_that_were_reversed_afterwards_reversed() -> None:
    _make_paid_order("o6", 1000)
    _refund("v6-r1", "o6", 300, "v6-k1")
    _refund("v6-r2", "o6", 200, "v6-k2")
    _advanced("v6-s", ["v6-r1", "v6-r2"], 500, "o6")
    # refund reversal acts on the refund itself; the claim stays until revoke
    assert client.post("/refunds/v6-r1/reverse",
                       headers={**H, "Idempotency-Key": "v6-r1-rev"}).status_code == 200
    assert _revoke("v6-s", "v6-rev").status_code == 200
    assert _refund_status("v6-r1") == "reversed"   # untouched by revoke
    assert _refund_status("v6-r2") == "pending"
    assert _claim_count("v6-r1") == 0 and _claim_count("v6-r2") == 0
    # the reversed refund cannot be claimed by a new settlement either
    assert _settle("v6-sb", "o6", 500, ["v6-r1", "v6-r2"], "v6-sb-accept").status_code == 201
    blocked = _advance("v6-sb", "v6-sb-adv")
    assert blocked.status_code == 409 and blocked.json()["detail"] == "referenced refund is reversed"
    # revoke did not substitute for reversal, and reversal does not restore the settlement
    assert client.get("/settlements/v6-s", headers=H).json()["status"] == "revoked"


# ---------- idempotent replay ----------

def test_revoke_replay_returns_first_result_and_flips_once() -> None:
    _make_paid_order("o7", 500)
    _refund("v7-r1", "o7", 500, "v7-k1")
    _advanced("v7-s", ["v7-r1"], 500, "o7")
    a = _revoke("v7-s", "same-rev-key")
    b = _revoke("v7-s", "same-rev-key")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE tenant=? AND target_id='v7-s' AND operation='settle_revoke'",
            (TENANT,),
        ).fetchone()["c"]
        claims = conn.execute(
            "SELECT COUNT(*) c FROM settlement_claims WHERE tenant=? AND refund_id='v7-r1'",
            (TENANT,),
        ).fetchone()["c"]
        flips = conn.execute(
            "SELECT COUNT(*) c FROM refunds WHERE tenant=? AND refund_id='v7-r1' AND status='pending'",
            (TENANT,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 1 and claims == 0 and flips == 1


def test_failed_revoke_is_replayed_identically_even_when_state_changes() -> None:
    # first request targets a settlement that does not exist yet
    first = _revoke("v8-s", "v8-rev")
    assert first.status_code == 404
    _make_paid_order("o8", 500)
    _refund("v8-r1", "o8", 100, "v8-k1")
    _advanced("v8-s", ["v8-r1"], 100, "o8")
    # the same fingerprint still replays the original 404 and changes nothing
    replay = _revoke("v8-s", "v8-rev")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert client.get("/settlements/v8-s", headers=H).json()["status"] == "effective"
    assert _claim_count("v8-r1") == 1
    # a different fingerprint evaluates current state and succeeds
    assert _revoke("v8-s", "v8-rev-retry").status_code == 200

    # a recorded 409 (revoke while pending) replays identically after the settlement later advances
    _make_paid_order("o8b", 500)
    _refund("v8b-r1", "o8b", 100, "v8b-k1")
    _settle("v8-sb", "o8b", 100, ["v8b-r1"], "v8-sb-accept")
    early = _revoke("v8-sb", "v8-sb-rev")
    assert early.status_code == 409
    assert _advance("v8-sb", "v8-sb-adv").status_code == 200
    replay2 = _revoke("v8-sb", "v8-sb-rev")
    assert replay2.status_code == 409 and replay2.json() == early.json()
    assert client.get("/settlements/v8-sb", headers=H).json()["status"] == "effective"


# ---------- concurrency ----------

def test_concurrent_revokes_distinct_keys_only_one_wins() -> None:
    _make_paid_order("o9", 1000)
    _refund("v9-r1", "o9", 100, "v9-k1")
    _advanced("v9-s", ["v9-r1"], 100, "o9")
    codes: list[int] = []

    def run(key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_revoke("v9-s", key).status_code)

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(f"v9-rev-{i}", barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 5
    assert client.get("/settlements/v9-s", headers=H).json()["status"] == "revoked"
    assert _refund_status("v9-r1") == "pending"
    assert _claim_count("v9-r1") == 0


def test_concurrent_revoke_and_advance_on_effective_settlement_only_one_takes_effect() -> None:
    _make_paid_order("o10", 1000)
    _refund("v10-r1", "o10", 100, "v10-k1")
    _advanced("v10-s", ["v10-r1"], 100, "o10")
    codes: list[tuple[str, int]] = []

    def run(kind: str, key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _revoke("v10-s", key) if kind == "revoke" else _advance("v10-s", key)
        codes.append((kind, resp.status_code))

    barrier = threading.Barrier(8)
    threads = []
    for i in range(4):
        threads.append(threading.Thread(target=run, args=("revoke", f"v10-rev-{i}", barrier)))
        threads.append(threading.Thread(target=run, args=("advance", f"v10-adv-{i}", barrier)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # the settlement is already effective: advances are always rejected and
    # exactly one revoke wins — no second state flip.
    assert sorted(code for _, code in codes).count(200) == 1
    assert all(code == 409 for kind, code in codes if kind == "advance")
    assert client.get("/settlements/v10-s", headers=H).json()["status"] == "revoked"
    assert _claim_count("v10-r1") == 0


def test_concurrent_revoke_and_advance_on_shared_refunds_never_double_claims() -> None:
    _make_paid_order("o11", 1000)
    _refund("v11-r1", "o11", 300, "v11-k1")
    _advanced("v11-sa", ["v11-r1"], 300, "o11")
    _settle("v11-sb", "o11", 300, ["v11-r1"], "v11-sb-accept")
    results: list[tuple[str, int]] = []

    def run(kind: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        if kind == "revoke":
            resp = _revoke("v11-sa", "v11-sa-rev")
        else:
            resp = _advance("v11-sb", "v11-sb-adv")
        results.append((kind, resp.status_code))

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=run, args=("revoke", barrier))
    t2 = threading.Thread(target=run, args=("advance", barrier))
    t1.start(); t2.start()
    t1.join(); t2.join()
    # uniqueness invariant under every serialization order:
    # advance-first loses to the existing claim; revoke-first frees it for the advance
    assert _claim_count("v11-r1") <= 1
    sb_status = client.get("/settlements/v11-sb", headers=H).json()["status"]
    sa_status = client.get("/settlements/v11-sa", headers=H).json()["status"]
    if sb_status == "effective":
        assert sa_status == "revoked"
        assert _refund_status("v11-r1") == "effective"
        assert dict(results)["advance"] == 200
    else:
        assert sb_status == "pending" and sa_status == "revoked"
        assert _refund_status("v11-r1") == "pending"
        assert dict(results)["advance"] == 409


# ---------- conservation ----------

def test_refundable_quota_conservation_through_revoke() -> None:
    _make_paid_order("o12", 1000)
    assert _refund("v12-a", "o12", 300, "v12-k1").status_code == 201
    assert _refund("v12-b", "o12", 200, "v12-k2").status_code == 201
    _advanced("v12-s", ["v12-a", "v12-b"], 500, "o12")
    # effective->pending flip on revoke does not change the occupied total
    assert _revoke("v12-s", "v12-rev").status_code == 200
    assert _refund("v12-c", "o12", 500, "v12-k3").status_code == 201
    assert _refund("v12-d", "o12", 1, "v12-k4").status_code == 409  # no room left

    conn = connect()
    try:
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id='o12'",
            (TENANT,),
        ).fetchone()
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
            "WHERE tenant=? AND order_id='o12' AND status IN ('pending','effective')",
            (TENANT,),
        ).fetchone()["s"]
    finally:
        conn.close()
    assert occupied == 1000
    assert order["paid_cents"] - occupied == 0
    assert occupied <= order["paid_cents"] <= order["amount_cents"]

    # re-settle the freed refunds with a new settlement; occupancy total unchanged
    _advanced("v12-sb", ["v12-a", "v12-b"], 500, "o12")
    conn = connect()
    try:
        occupied2 = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
            "WHERE tenant=? AND order_id='o12' AND status IN ('pending','effective')",
            (TENANT,),
        ).fetchone()["s"]
    finally:
        conn.close()
    assert occupied2 == 1000


# ---------- durability across restart ----------

def test_revoke_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-revoke.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders, refunds, settlements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('rv','rro',1000,'CNY')\n"
        "    orders.add_payment('rv','rro',1000)\n"
        "    refunds.accept('rv','rrr','rro',250,'why','rr-key')\n"
        "    settlements.accept('rv','rrs','rro',250,'周期结算',['rrr'],'rs-accept-key')\n"
        "    settlements.advance('rv','rrs','rs-adv-key')\n"
        "out = settlements.revoke('rv','rrs','rs-rev-key')\n"
        "conn = connect()\n"
        "claims = conn.execute(\"SELECT COUNT(*) FROM settlement_claims WHERE refund_id='rrr'\").fetchone()[0]\n"
        "status = conn.execute(\"SELECT status FROM refunds WHERE refund_id='rrr'\").fetchone()[0]\n"
        "settlement = conn.execute(\"SELECT status FROM settlements WHERE settlement_id='rrs'\").fetchone()[0]\n"
        "conn.close()\n"
        "print(json.dumps({'http': out.status, 'settlement': settlement,\n"
        "                  'refund': status, 'claims': claims}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b == {"http": 200, "settlement": "revoked", "refund": "pending", "claims": 0}
