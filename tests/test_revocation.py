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


def _reverse_refund(refund_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/refunds/{refund_id}/reverse",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _refund_status(refund_id: str, tenant: str = TENANT) -> str:
    return client.get(f"/refunds/{refund_id}", headers={"X-Tenant": tenant}).json()["status"]


def _settlement_status(settlement_id: str, tenant: str = TENANT) -> str:
    return client.get(f"/settlements/{settlement_id}",
                      headers={"X-Tenant": tenant}).json()["status"]


def _claim_count(refund_id: str) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM settlement_claims WHERE refund_id=?",
            (refund_id,),
        ).fetchone()["c"]
    finally:
        conn.close()


def _advance_settlement(settlement_id: str, refund_ids: list[str], order_id: str,
                        amount: int) -> None:
    """Accept and advance a settlement over the given refunds."""
    assert _settle(settlement_id, order_id, amount, refund_ids, f"{settlement_id}-accept").status_code == 201
    assert _advance(settlement_id, f"{settlement_id}-adv").status_code == 200


# ---------- basic revocation ----------

def test_revoke_lifts_claims_and_returns_refunds_to_pending() -> None:
    _make_paid_order("o1", 1000)
    _refund("v1-r1", "o1", 300, "v1-k1")
    _refund("v1-r2", "o1", 200, "v1-k2")
    _advance_settlement("v1-s", ["v1-r1", "v1-r2"], "o1", 500)
    assert _claim_count("v1-r1") == 1 and _claim_count("v1-r2") == 1

    resp = _revoke("v1-s", "v1-rev")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"settlement_id", "order_id", "amount_cents", "refund_ids",
                         "reason", "status", "created_at", "updated_at"}
    assert body["status"] == "revoked"
    assert body["refund_ids"] == ["v1-r1", "v1-r2"]
    assert body["updated_at"] >= body["created_at"]

    # settlement reaches the revoked terminal state ...
    assert _settlement_status("v1-s") == "revoked"
    # ... claims are gone ...
    assert _claim_count("v1-r1") == 0 and _claim_count("v1-r2") == 0
    # ... and refunds are pending again (NOT reversed): they still occupy quota.
    assert _refund_status("v1-r1") == "pending"
    assert _refund_status("v1-r2") == "pending"
    got = client.get("/settlements/v1-s", headers=H).json()
    assert got == body


def test_revoke_pending_settlement_conflicts() -> None:
    _make_paid_order("o2", 500)
    _refund("v2-r1", "o2", 100, "v2-k1")
    _settle("v2-s", "o2", 100, ["v2-r1"], "v2-accept")
    resp = _revoke("v2-s", "v2-rev")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settlement is not effective"
    # nothing moved
    assert _settlement_status("v2-s") == "pending"
    assert _refund_status("v2-r1") == "pending"
    assert _claim_count("v2-r1") == 0


def test_duplicate_revoke_conflicts_and_keeps_state() -> None:
    _make_paid_order("o3", 500)
    _refund("v3-r1", "o3", 100, "v3-k1")
    _advance_settlement("v3-s", ["v3-r1"], "o3", 100)
    first = _revoke("v3-s", "v3-rev-a")
    assert first.status_code == 200 and first.json()["status"] == "revoked"
    # a different request against the same (now revoked) settlement is a duplicate revoke
    again = _revoke("v3-s", "v3-rev-b")
    assert again.status_code == 409
    assert again.json()["detail"] == "settlement already revoked"
    assert _settlement_status("v3-s") == "revoked"
    assert _refund_status("v3-r1") == "pending"
    assert _claim_count("v3-r1") == 0


def test_revoked_settlement_cannot_advance() -> None:
    _make_paid_order("o4", 500)
    _refund("v4-r1", "o4", 100, "v4-k1")
    _advance_settlement("v4-s", ["v4-r1"], "o4", 100)
    assert _revoke("v4-s", "v4-rev").status_code == 200
    resp = _advance("v4-s", "v4-adv-again")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settlement already revoked"
    assert _settlement_status("v4-s") == "revoked"


def test_revoke_missing_or_cross_tenant_is_not_found() -> None:
    assert _revoke("ghost-settlement", "ghost-rev").status_code == 404
    # no existence leaked
    assert client.get("/settlements/ghost-settlement", headers=H).status_code == 404

    _make_paid_order("o5", 500)
    _refund("v5-r1", "o5", 100, "v5-k1")
    _advance_settlement("v5-s", ["v5-r1"], "o5", 100)
    assert _revoke("v5-s", "v5-rev", tenant="t2").status_code == 404
    # cross-tenant attempt changed nothing
    assert _settlement_status("v5-s") == "effective"


def test_revoke_requires_tenant_and_idempotency_headers() -> None:
    no_tenant = client.post("/settlements/whatever/revoke",
                            headers={"Idempotency-Key": "k"})
    assert no_tenant.status_code == 400
    no_key = client.post("/settlements/whatever/revoke", headers=H)
    assert no_key.status_code == 400


# ---------- atomicity ----------

def test_revoke_is_atomic_when_a_settled_refund_was_reversed() -> None:
    _make_paid_order("o6", 1000)
    _refund("v6-r1", "o6", 300, "v6-k1")
    _refund("v6-r2", "o6", 200, "v6-k2")
    _advance_settlement("v6-s", ["v6-r1", "v6-r2"], "o6", 500)

    # reversing one settled refund (acts on the refund only; claim row is untouched)
    assert _reverse_refund("v6-r2", "v6-rev-r2").status_code == 200
    assert _refund_status("v6-r2") == "reversed"

    resp = _revoke("v6-s", "v6-rev")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "settled refund has been reversed"

    # all-or-nothing: settlement effective, no claims lifted, no refund flipped
    assert _settlement_status("v6-s") == "effective"
    assert _refund_status("v6-r1") == "effective"
    assert _refund_status("v6-r2") == "reversed"
    assert _claim_count("v6-r1") == 1 and _claim_count("v6-r2") == 1


def test_revoke_does_not_substitute_for_refund_reverse() -> None:
    _make_paid_order("o7", 500)
    _refund("v7-r1", "o7", 100, "v7-k1")
    _advance_settlement("v7-s", ["v7-r1"], "o7", 100)
    assert _revoke("v7-s", "v7-rev").status_code == 200
    # revoke lifted the核销 relationship only: refund is pending, not reversed ...
    assert _refund_status("v7-r1") == "pending"
    # ... and refund reversal remains an independent operation on the refund itself
    assert _reverse_refund("v7-r1", "v7-rev-r1").status_code == 200
    assert _refund_status("v7-r1") == "reversed"
    assert _settlement_status("v7-s") == "revoked"


# ---------- re-settlement after revocation ----------

def test_revoked_refunds_can_be_settled_by_another_settlement() -> None:
    _make_paid_order("o8", 1000)
    _refund("v8-r1", "o8", 300, "v8-k1")
    # two pending settlements reference the same refund
    _settle("v8-a", "o8", 300, ["v8-r1"], "v8a-accept")
    _settle("v8-b", "o8", 300, ["v8-r1"], "v8b-accept")
    assert _advance("v8-a", "v8a-adv").status_code == 200
    # b cannot claim while a holds the refund
    blocked = _advance("v8-b", "v8b-adv")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "referenced refund already settled by another settlement"

    assert _revoke("v8-a", "v8a-revoke").status_code == 200
    assert _refund_status("v8-r1") == "pending"
    # now b can claim the released refund
    assert _advance("v8-b", "v8b-adv-retry").status_code == 200
    assert _settlement_status("v8-a") == "revoked"
    assert _settlement_status("v8-b") == "effective"
    assert _refund_status("v8-r1") == "effective"
    # uniqueness still holds: exactly one claim, now held by b
    assert _claim_count("v8-r1") == 1
    conn = connect()
    try:
        holder = conn.execute(
            "SELECT settlement_id FROM settlement_claims WHERE refund_id=?",
            ("v8-r1",),
        ).fetchone()["settlement_id"]
    finally:
        conn.close()
    assert holder == "v8-b"


# ---------- idempotent replay ----------

def test_revoke_replay_returns_first_result_and_flips_once() -> None:
    _make_paid_order("o9", 500)
    _refund("v9-r1", "o9", 100, "v9-k1")
    _advance_settlement("v9-s", ["v9-r1"], "o9", 100)
    a = _revoke("v9-s", "same-rev-key")
    b = _revoke("v9-s", "same-rev-key")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id=? AND operation='settle_revoke'", ("v9-s",),
        ).fetchone()["c"]
        claims = conn.execute(
            "SELECT COUNT(*) c FROM settlement_claims WHERE refund_id=?", ("v9-r1",),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 1 and claims == 0


def test_failed_revoke_is_replayed_identically_even_when_now_revocable() -> None:
    _make_paid_order("o10", 500)
    _refund("v10-r1", "o10", 100, "v10-k1")
    _settle("v10-s", "o10", 100, ["v10-r1"], "v10-accept")
    first = _revoke("v10-s", "v10-rev")
    assert first.status_code == 409  # still pending -> not effective
    # the settlement later becomes effective, but the same fingerprint replays the old 409
    assert _advance("v10-s", "v10-adv").status_code == 200
    replay = _revoke("v10-s", "v10-rev")
    assert replay.status_code == 409 and replay.json() == first.json()
    assert _settlement_status("v10-s") == "effective"
    assert _claim_count("v10-r1") == 1
    # a different fingerprint evaluates against current state and now revokes
    assert _revoke("v10-s", "v10-rev-retry").status_code == 200
    assert _settlement_status("v10-s") == "revoked"


def test_not_found_revoke_is_replayed_identically() -> None:
    first = _revoke("v11-ghost", "v11-rev")
    assert first.status_code == 404
    replay = _revoke("v11-ghost", "v11-rev")
    assert replay.status_code == 404 and replay.json() == first.json()


# ---------- concurrency ----------

def test_concurrent_revokes_distinct_keys_only_one_wins() -> None:
    _make_paid_order("o12", 1000)
    _refund("v12-r1", "o12", 300, "v12-k1")
    _refund("v12-r2", "o12", 200, "v12-k2")
    _advance_settlement("v12-s", ["v12-r1", "v12-r2"], "o12", 500)
    codes: list[int] = []

    def run(key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_revoke("v12-s", key).status_code)

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(f"v12-rev-{i}", barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 5
    assert _settlement_status("v12-s") == "revoked"
    assert _refund_status("v12-r1") == "pending"
    assert _refund_status("v12-r2") == "pending"
    assert _claim_count("v12-r1") == 0 and _claim_count("v12-r2") == 0


def test_concurrent_advance_and_revoke_on_one_settlement_only_one_takes_effect() -> None:
    _make_paid_order("o13", 500)
    _refund("v13-r1", "o13", 100, "v13-k1")
    _advance_settlement("v13-s", ["v13-r1"], "o13", 100)  # starts effective
    results: list[tuple[str, int]] = []

    def run(kind: str, key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        if kind == "revoke":
            results.append((kind, _revoke("v13-s", key).status_code))
        else:
            results.append((kind, _advance("v13-s", key).status_code))

    # 3 revokers + 3 advancers released together; regardless of serialization
    # order exactly one operation (the first revoke) takes effect.
    barrier = threading.Barrier(6)
    threads = []
    for i in range(3):
        threads.append(threading.Thread(target=run, args=("revoke", f"v13-rev-{i}", barrier)))
        threads.append(threading.Thread(target=run, args=("advance", f"v13-adv-{i}", barrier)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [kind for kind, code in results if code == 200]
    assert successes == ["revoke"], results
    assert _settlement_status("v13-s") == "revoked"
    assert _refund_status("v13-r1") == "pending"
    assert _claim_count("v13-r1") == 0


def test_concurrent_revoke_and_other_settlement_advance_keep_claims_consistent() -> None:
    # revoke(v14-a) and advance(v14-b) race over the same refund batch.
    # Either serialization is valid; in every outcome the refund has at most one
    # effective claimant and no partial/corrupt state remains.
    _make_paid_order("o14", 500)
    _refund("v14-r1", "o14", 100, "v14-k1")
    _advance_settlement("v14-a", ["v14-r1"], "o14", 100)
    _settle("v14-b", "o14", 100, ["v14-r1"], "v14b-accept")
    outcome: dict[str, int] = {}

    def run(kind: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        if kind == "revoke":
            outcome[kind] = _revoke("v14-a", "v14a-revoke").status_code
        else:
            outcome[kind] = _advance("v14-b", "v14b-adv").status_code

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=run, args=("revoke", barrier))
    t2 = threading.Thread(target=run, args=("advance", barrier))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert 500 not in outcome.values()
    assert outcome["revoke"] == 200  # revoking the effective settlement always lands
    assert _settlement_status("v14-a") == "revoked"
    if outcome["advance"] == 200:
        # revoke landed first: b claimed the released refund
        assert _settlement_status("v14-b") == "effective"
        assert _refund_status("v14-r1") == "effective"
        assert _claim_count("v14-r1") == 1
    else:
        # advance landed first and was rejected as already-settled; revoke then released it
        assert outcome["advance"] == 409
        assert _settlement_status("v14-b") == "pending"
        assert _refund_status("v14-r1") == "pending"
        assert _claim_count("v14-r1") == 0


# ---------- conservation ----------

def test_revocation_preserves_refundable_quota_conservation() -> None:
    _make_paid_order("o15", 1000)
    assert _refund("v15-a", "o15", 400, "v15-k1").status_code == 201
    assert _refund("v15-b", "o15", 200, "v15-k2").status_code == 201
    _advance_settlement("v15-s", ["v15-a", "v15-b"], "o15", 600)

    def refundable() -> tuple[int, int, int]:
        conn = connect()
        try:
            order = conn.execute(
                "SELECT amount_cents, paid_cents FROM orders WHERE order_id='o15'").fetchone()
            occupied = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
                "WHERE order_id='o15' AND status IN ('pending','effective')").fetchone()["s"]
        finally:
            conn.close()
        return order["amount_cents"], order["paid_cents"], occupied

    amount, paid, occupied = refundable()
    assert occupied == 600 and paid - occupied == 400

    assert _revoke("v15-s", "v15-rev").status_code == 200
    # effective -> pending does not change occupancy: quota is NOT released
    amount, paid, occupied = refundable()
    assert occupied == 600 and paid - occupied == 400
    assert occupied <= paid <= amount

    # the still-occupied quota after revocation is unchanged
    assert _refund("v15-c", "o15", 401, "v15-k3").status_code == 409
    assert _refund("v15-c", "o15", 400, "v15-k4").status_code == 201

    # the released claims can be re-settled; conservation still holds afterwards
    _settle("v15-s2", "o15", 400, ["v15-a"], "v15s2-accept")
    assert _advance("v15-s2", "v15s2-adv").status_code == 200
    amount, paid, occupied = refundable()
    assert occupied == 1000 and paid - occupied == 0
    assert occupied <= paid <= amount


# ---------- durability across restart ----------

def test_revoke_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-revocation.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import sys, json\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders, refunds, settlements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('rv','ro',1000,'CNY')\n"
        "    orders.add_payment('rv','ro',1000)\n"
        "    refunds.accept('rv','rr','ro',250,'why','r-key')\n"
        "    settlements.accept('rv','rs','ro',250,'周期结算',['rr'],'s-accept-key')\n"
        "    settlements.advance('rv','rs','s-adv-key')\n"
        "out = settlements.revoke('rv','rs','s-revoke-key')\n"
        "conn = connect()\n"
        "claims = conn.execute(\"SELECT COUNT(*) FROM settlement_claims WHERE refund_id='rr'\").fetchone()[0]\n"
        "status = conn.execute(\"SELECT status FROM refunds WHERE refund_id='rr'\").fetchone()[0]\n"
        "settlement = conn.execute(\"SELECT status FROM settlements WHERE settlement_id='rs'\").fetchone()[0]\n"
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
