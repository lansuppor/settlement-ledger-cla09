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
TENANT = "st"
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


def _refund_status(refund_id: str, tenant: str = TENANT) -> str:
    return client.get(f"/refunds/{refund_id}", headers={"X-Tenant": tenant}).json()["status"]


# ---------- accept & read ----------

def test_accept_settlement_is_pending_and_returns_fields() -> None:
    _make_paid_order("o1", 1000)
    _refund("s1-r1", "o1", 100, "s1-k1")
    _refund("s1-r2", "o1", 200, "s1-k2")
    resp = _settle("s1", "o1", 300, ["s1-r1", "s1-r2"], "s1-accept")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"settlement_id", "order_id", "amount_cents", "refund_ids",
                         "reason", "status", "created_at", "updated_at"}
    assert body["settlement_id"] == "s1" and body["order_id"] == "o1"
    assert body["amount_cents"] == 300 and body["status"] == "pending"
    assert body["refund_ids"] == ["s1-r1", "s1-r2"]
    assert body["created_at"] == body["updated_at"]

    got = client.get("/settlements/s1", headers=H)
    assert got.status_code == 200 and got.json() == body


def test_read_settlement_is_tenant_isolated() -> None:
    _make_paid_order("o2", 500)
    _refund("s2-r1", "o2", 100, "s2-k1")
    _settle("s2", "o2", 100, ["s2-r1"], "s2-accept")
    assert client.get("/settlements/s2", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/settlements/s2").status_code == 400
    assert _advance("s2", "s2-adv-x", tenant="t2").status_code == 404


def test_accept_against_missing_or_cross_tenant_order_is_not_found() -> None:
    assert _settle("s3", "no-such-order", 10, ["whatever"], "s3-accept").status_code == 404

    _make_paid_order("foreign-o", 500, tenant="t2")
    resp = _settle("s3b", "foreign-o", 10, ["whatever"], "s3b-accept", tenant="t1")
    assert resp.status_code == 404
    # no object existence leaked: neither settlement was created
    assert client.get("/settlements/s3", headers=H).status_code == 404
    assert client.get("/settlements/s3b", headers={"X-Tenant": "t1"}).status_code == 404


def test_duplicate_accept_conflicts_and_keeps_first() -> None:
    _make_paid_order("o4", 500)
    _refund("s4-r1", "o4", 100, "s4-k1")
    first = _settle("s4", "o4", 100, ["s4-r1"], "s4-accept-a")
    assert first.status_code == 201
    # a different request against the same settlement identifier is a duplicate accept
    dup = _settle("s4", "o4", 100, ["s4-r1"], "s4-accept-b")
    assert dup.status_code == 409 and dup.json()["detail"] == "settlement already accepted"
    # original snapshot untouched
    assert client.get("/settlements/s4", headers=H).json()["refund_ids"] == ["s4-r1"]


def test_invalid_accept_params() -> None:
    _make_paid_order("o5", 500)
    _refund("s5-r1", "o5", 100, "s5-k1")
    # boundary validation happens at the request edge
    bad_amount = client.post(
        "/settlements",
        json={"settlement_id": "s5a", "order_id": "o5", "amount_cents": 0,
              "refund_ids": ["s5-r1"]},
        headers={**H, "Idempotency-Key": "s5a-accept"},
    )
    assert bad_amount.status_code == 422
    empty_refs = client.post(
        "/settlements",
        json={"settlement_id": "s5b", "order_id": "o5", "amount_cents": 100,
              "refund_ids": []},
        headers={**H, "Idempotency-Key": "s5b-accept"},
    )
    assert empty_refs.status_code == 422
    # duplicated identifiers within one reference set are a 400
    dup_ref = _settle("s5c", "o5", 200, ["s5-r1", "s5-r1"], "s5c-accept")
    assert dup_ref.status_code == 400
    # missing required headers
    no_tenant = client.post(
        "/settlements",
        json={"settlement_id": "s5d", "order_id": "o5", "amount_cents": 100,
              "refund_ids": ["s5-r1"]},
        headers={"Idempotency-Key": "s5d-accept"},
    )
    assert no_tenant.status_code == 400
    no_key = client.post(
        "/settlements",
        json={"settlement_id": "s5e", "order_id": "o5", "amount_cents": 100,
              "refund_ids": ["s5-r1"]},
        headers=H,
    )
    assert no_key.status_code == 400
    # rejected accepts leave nothing behind
    for sid in ("s5a", "s5b", "s5c", "s5d", "s5e"):
        assert client.get(f"/settlements/{sid}", headers=H).status_code == 404


# ---------- advance ----------

def test_advance_flips_all_refunds_and_settlement() -> None:
    _make_paid_order("o6", 1000)
    _refund("s6-r1", "o6", 300, "s6-k1")
    _refund("s6-r2", "o6", 200, "s6-k2")
    accepted = _settle("s6", "o6", 500, ["s6-r1", "s6-r2"], "s6-accept").json()

    resp = _advance("s6", "s6-adv")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "effective"
    assert body["refund_ids"] == ["s6-r1", "s6-r2"]
    assert body["updated_at"] >= accepted["updated_at"]
    assert _refund_status("s6-r1") == "effective"
    assert _refund_status("s6-r2") == "effective"
    assert client.get("/settlements/s6", headers=H).json()["status"] == "effective"


def test_advance_missing_settlement_is_not_found() -> None:
    assert _advance("ghost-settlement", "ghost-adv").status_code == 404


def test_advance_referenced_refund_missing_leaves_everything_unchanged() -> None:
    _make_paid_order("o7", 1000)
    _refund("s7-r1", "o7", 100, "s7-k1")
    _settle("s7", "o7", 200, ["s7-r1", "s7-never"], "s7-accept")
    resp = _advance("s7", "s7-adv")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "referenced refund not found"
    # nothing moved: settlement pending, the existing refund still pending
    assert client.get("/settlements/s7", headers=H).json()["status"] == "pending"
    assert _refund_status("s7-r1") == "pending"


def test_advance_referenced_refund_of_another_order_is_not_found() -> None:
    _make_paid_order("o8a", 1000)
    _make_paid_order("o8b", 1000)
    _refund("s8-r1", "o8a", 100, "s8-k1")
    _refund("s8-r2", "o8b", 100, "s8-k2")  # exists within the tenant, but another order
    _settle("s8", "o8a", 200, ["s8-r1", "s8-r2"], "s8-accept")
    resp = _advance("s8", "s8-adv")
    assert resp.status_code == 404 and resp.json()["detail"] == "referenced refund not found"
    assert client.get("/settlements/s8", headers=H).json()["status"] == "pending"
    assert _refund_status("s8-r1") == "pending"
    assert _refund_status("s8-r2") == "pending"


def test_advance_reversed_refund_conflicts_without_change() -> None:
    _make_paid_order("o9", 500)
    _refund("s9-r1", "o9", 100, "s9-k1")
    _refund("s9-r2", "o9", 100, "s9-k2")
    client.post("/refunds/s9-r2/reverse",
                headers={**H, "Idempotency-Key": "s9-rev"})
    _settle("s9", "o9", 200, ["s9-r1", "s9-r2"], "s9-accept")
    resp = _advance("s9", "s9-adv")
    assert resp.status_code == 409 and resp.json()["detail"] == "referenced refund is reversed"
    assert client.get("/settlements/s9", headers=H).json()["status"] == "pending"
    assert _refund_status("s9-r1") == "pending"
    assert _refund_status("s9-r2") == "reversed"


def test_advance_amount_mismatch_conflicts_without_change() -> None:
    _make_paid_order("o10", 1000)
    _refund("s10-r1", "o10", 300, "s10-k1")
    _refund("s10-r2", "o10", 300, "s10-k2")
    _settle("s10", "o10", 500, ["s10-r1", "s10-r2"], "s10-accept")  # refs total 600
    resp = _advance("s10", "s10-adv")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settlement amount does not match referenced refunds total"
    assert client.get("/settlements/s10", headers=H).json()["status"] == "pending"
    assert _refund_status("s10-r1") == "pending"
    assert _refund_status("s10-r2") == "pending"


def test_readvance_effective_settlement_conflicts() -> None:
    _make_paid_order("o11", 500)
    _refund("s11-r1", "o11", 500, "s11-k1")
    _settle("s11", "o11", 500, ["s11-r1"], "s11-accept")
    assert _advance("s11", "s11-adv-a").status_code == 200
    again = _advance("s11", "s11-adv-b")  # different request, same target
    assert again.status_code == 409 and again.json()["detail"] == "settlement already advanced"
    assert client.get("/settlements/s11", headers=H).json()["status"] == "effective"


def test_refund_settled_by_one_cannot_be_settled_again() -> None:
    _make_paid_order("o12", 1000)
    _refund("s12-r1", "o12", 400, "s12-k1")
    # two pending settlements may both reference the same refund while pending ...
    _settle("s12a", "o12", 400, ["s12-r1"], "s12a-accept")
    _settle("s12b", "o12", 400, ["s12-r1"], "s12b-accept")
    # ... but only one advance wins; the other fails with a distinguishable conflict
    assert _advance("s12a", "s12a-adv").status_code == 200
    loser = _advance("s12b", "s12b-adv")
    assert loser.status_code == 409
    assert loser.json()["detail"] == "referenced refund already settled by another settlement"
    # loser's own state is untouched, winner's refund flipped exactly once
    assert client.get("/settlements/s12b", headers=H).json()["status"] == "pending"
    assert client.get("/settlements/s12a", headers=H).json()["status"] == "effective"
    assert _refund_status("s12-r1") == "effective"
    conn = connect()
    try:
        claims = conn.execute(
            "SELECT COUNT(*) c FROM settlement_claims WHERE refund_id='s12-r1'").fetchone()["c"]
    finally:
        conn.close()
    assert claims == 1


# ---------- idempotent replay ----------

def test_advance_replay_returns_first_result_and_flips_once() -> None:
    _make_paid_order("o13", 500)
    _refund("s13-r1", "o13", 500, "s13-k1")
    _settle("s13", "o13", 500, ["s13-r1"], "s13-accept")
    a = _advance("s13", "same-adv-key")
    b = _advance("s13", "same-adv-key")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='s13' AND operation='settle_advance'").fetchone()["c"]
        claims = conn.execute(
            "SELECT COUNT(*) c FROM settlement_claims WHERE refund_id='s13-r1'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1 and claims == 1


def test_accept_replay_returns_first_result_even_conflicting_now() -> None:
    _make_paid_order("o14", 500)
    _refund("s14-r1", "o14", 100, "s14-k1")
    first = _settle("s14", "o14", 100, ["s14-r1"], "same-accept-key")
    replay = _settle("s14", "o14", 100, ["s14-r1"], "same-accept-key")
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()


def test_failed_advance_is_replayed_identically() -> None:
    _make_paid_order("o15", 1000)
    _refund("s15-r1", "o15", 100, "s15-k1")
    _settle("s15", "o15", 200, ["s15-r1", "s15-missing"], "s15-accept")
    first = _advance("s15", "s15-adv")
    assert first.status_code == 404
    # even after the missing refund later appears, the same fingerprint replays the old 404
    _refund("s15-missing", "o15", 100, "s15-k2")
    replay = _advance("s15", "s15-adv")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert client.get("/settlements/s15", headers=H).json()["status"] == "pending"
    # a different fingerprint re-validates against current state and now succeeds
    assert _advance("s15", "s15-adv-retry").status_code == 200


def test_concurrent_advances_distinct_keys_only_one_wins() -> None:
    _make_paid_order("o16", 1000)
    _refund("s16-r1", "o16", 100, "s16-k1")
    _settle("s16", "o16", 100, ["s16-r1"], "s16-accept")
    codes: list[int] = []

    def run(key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_advance("s16", key).status_code)

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(f"s16-adv-{i}", barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 5
    assert _refund_status("s16-r1") == "effective"
    conn = connect()
    try:
        claims = conn.execute(
            "SELECT COUNT(*) c FROM settlement_claims WHERE refund_id='s16-r1'").fetchone()["c"]
    finally:
        conn.close()
    assert claims == 1


# ---------- conservation ----------

def test_refundable_quota_conservation_through_settlement_lifecycle() -> None:
    _make_paid_order("o17", 1000)
    assert _refund("s17-a", "o17", 300, "s17-k1").status_code == 201
    assert _refund("s17-b", "o17", 200, "s17-k2").status_code == 201
    _settle("s17", "o17", 500, ["s17-a", "s17-b"], "s17-accept")
    assert _advance("s17", "s17-adv").status_code == 200

    # advancing flips pending->effective: occupancy total is unchanged at 500
    assert _refund("s17-c", "o17", 500, "s17-k3").status_code == 201
    assert _refund("s17-d", "o17", 1, "s17-k4").status_code == 409  # no room left

    # reversing an effective refund referenced by the settlement releases quota
    client.post("/refunds/s17-a/reverse", headers={**H, "Idempotency-Key": "s17-rev-a"})

    conn = connect()
    try:
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE order_id='o17'").fetchone()
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
            "WHERE order_id='o17' AND status IN ('pending','effective')").fetchone()["s"]
    finally:
        conn.close()
    assert occupied == 700  # b effective 200 + c pending 500
    assert order["paid_cents"] - occupied == 300
    assert occupied <= order["paid_cents"] <= order["amount_cents"]
    # released quota is usable again
    assert _refund("s17-e", "o17", 300, "s17-k5").status_code == 201


# ---------- durability across restart ----------

def test_advance_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-settlement.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders, refunds, settlements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('st','ro',1000,'CNY')\n"
        "    orders.add_payment('st','ro',1000)\n"
        "    refunds.accept('st','rr','ro',250,'why','r-key')\n"
        "    settlements.accept('st','rs','ro',250,'周期结算',['rr'],'s-accept-key')\n"
        "out = settlements.advance('st','rs','s-adv-key')\n"
        "conn = connect()\n"
        "claims = conn.execute(\"SELECT COUNT(*) FROM settlement_claims WHERE refund_id='rr'\").fetchone()[0]\n"
        "status = conn.execute(\"SELECT status FROM refunds WHERE refund_id='rr'\").fetchone()[0]\n"
        "conn.close()\n"
        "print(json.dumps({'http': out.status, 'settlement': out.body.get('status'),\n"
        "                  'refund': status, 'claims': claims}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b == {"http": 200, "settlement": "effective", "refund": "effective", "claims": 1}
