import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_settlements.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store import settlements as settlement_store
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
# Dedicated tenant so this module never collides with the other test modules.
TENANT = "st"
H = {"X-Tenant": TENANT}


def _make_paid_order(order_id: str, amount: int = 1000, paid: int | None = None,
                     tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")
    order_store.add_payment(tenant, order_id, paid if paid is not None else amount)


def _refund(refund_id: str, order_id: str, amount: int, key: str, tenant: str = TENANT):
    return client.post(
        "/refunds",
        json={"refund_id": refund_id, "order_id": order_id,
              "amount_cents": amount, "reason": "customer request"},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _settle(settlement_id: str, order_id: str, amount: int, refund_ids: list[str],
            key: str, reason: str = "batch settlement", tenant: str = TENANT):
    return client.post(
        "/settlements",
        json={"settlement_id": settlement_id, "order_id": order_id,
              "amount_cents": amount, "refund_ids": refund_ids, "reason": reason},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _advance(settlement_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/settlements/{settlement_id}/advance",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


# ---------- accept & read ----------

def test_accept_settlement_is_pending_and_returns_fields() -> None:
    _make_paid_order("o1", 1000)
    _refund("sr1a", "o1", 200, "sk1a")
    _refund("sr1b", "o1", 100, "sk1b")
    resp = _settle("s1", "o1", 300, ["sr1a", "sr1b"], "skey1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {
        "settlement_id", "order_id", "amount_cents", "refund_ids",
        "reason", "status", "created_at", "updated_at",
    }
    assert body["settlement_id"] == "s1"
    assert body["order_id"] == "o1"
    assert body["amount_cents"] == 300
    assert body["refund_ids"] == ["sr1a", "sr1b"]
    assert body["status"] == "pending"
    assert body["created_at"] == body["updated_at"]

    got = client.get("/settlements/s1", headers=H)
    assert got.status_code == 200 and got.json() == body
    # accepting a settlement does not flip refunds yet
    assert client.get("/refunds/sr1a", headers=H).json()["status"] == "pending"


def test_read_is_tenant_isolated() -> None:
    _make_paid_order("o2", 500)
    _refund("sr2", "o2", 100, "sk2")
    _settle("s2", "o2", 100, ["sr2"], "skey2")
    assert client.get("/settlements/s2", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/settlements/s2").status_code == 400


def test_accept_against_missing_or_cross_tenant_order_is_not_found() -> None:
    assert _settle("s3", "no-such-order", 10, ["x"], "skey3").status_code == 404
    _make_paid_order("foreign-order-st", 500, tenant="t2")
    resp = _settle("s3b", "foreign-order-st", 10, ["x"], "skey3b", tenant="t1")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"


def test_duplicate_accept_conflicts() -> None:
    _make_paid_order("o4", 500)
    _refund("sr4", "o4", 100, "sk4")
    assert _settle("s4", "o4", 100, ["sr4"], "skey4a").status_code == 201
    dup = _settle("s4", "o4", 100, ["sr4"], "skey4b")
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "settlement_already_accepted"
    # only one row and still pending
    assert client.get("/settlements/s4", headers=H).json()["status"] == "pending"


def test_invalid_request_shapes_are_400_or_422() -> None:
    _make_paid_order("o5", 500)
    _refund("sr5", "o5", 100, "sk5")
    base = {"settlement_id": "s5", "order_id": "o5", "amount_cents": 100,
            "refund_ids": ["sr5"], "reason": ""}
    # missing idempotency key
    r = client.post("/settlements", json=base, headers=H)
    assert r.status_code == 400
    # non-positive amount -> 422 at the schema boundary
    r = client.post("/settlements", json={**base, "settlement_id": "s5b", "amount_cents": 0},
                    headers={**H, "Idempotency-Key": "skey5b"})
    assert r.status_code == 422
    # empty refund set -> 422 at the schema boundary
    r = client.post("/settlements", json={**base, "settlement_id": "s5c", "refund_ids": []},
                    headers={**H, "Idempotency-Key": "skey5c"})
    assert r.status_code == 422
    # duplicated refund ids in one request is a 400 business error
    r = _settle("s5d", "o5", 200, ["sr5", "sr5"], "skey5d")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_request"


# ---------- advance: success & atomicity ----------

def test_advance_flips_all_refunds_and_settlement_to_effective() -> None:
    _make_paid_order("o6", 1000)
    _refund("sr6a", "o6", 300, "sk6a")
    _refund("sr6b", "o6", 200, "sk6b")
    _settle("s6", "o6", 500, ["sr6a", "sr6b"], "skey6")
    before = client.get("/settlements/s6", headers=H).json()

    resp = _advance("s6", "akey6")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "effective"
    assert body["refund_ids"] == ["sr6a", "sr6b"]
    assert body["created_at"] == before["created_at"]
    assert body["updated_at"] >= before["updated_at"]

    assert client.get("/refunds/sr6a", headers=H).json()["status"] == "effective"
    assert client.get("/refunds/sr6b", headers=H).json()["status"] == "effective"
    assert client.get("/settlements/s6", headers=H).json()["status"] == "effective"


def test_advance_missing_settlement_is_not_found() -> None:
    assert _advance("ghost-settlement", "akeyghost").status_code == 404
    _make_paid_order("o7", 100)
    _refund("sr7", "o7", 10, "sk7")
    _settle("s7", "o7", 10, ["sr7"], "skey7")
    cross = _advance("s7", "akey7", tenant="t2")
    assert cross.status_code == 404


def test_advance_fails_when_referenced_refund_missing_and_changes_nothing() -> None:
    _make_paid_order("o8", 1000)
    # a refund referenced at accept time that never existed is tolerated at accept
    _settle("s8", "o8", 100, ["sr8-missing"], "skey8")
    resp = _advance("s8", "akey8")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not_found"
    assert client.get("/settlements/s8", headers=H).json()["status"] == "pending"


def test_advance_fails_on_reversed_refund_atomically() -> None:
    _make_paid_order("o9", 1000)
    _refund("sr9a", "o9", 300, "sk9a")
    _refund("sr9b", "o9", 200, "sk9b")
    _settle("s9", "o9", 500, ["sr9a", "sr9b"], "skey9")
    # reverse one of the referenced refunds before advancing
    client.post("/refunds/sr9b/reverse", headers={**H, "Idempotency-Key": "rev9b"})

    resp = _advance("s9", "akey9")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "refund_reversed"
    # atomicity: neither refund flipped, settlement still pending
    assert client.get("/refunds/sr9a", headers=H).json()["status"] == "pending"
    assert client.get("/refunds/sr9b", headers=H).json()["status"] == "reversed"
    assert client.get("/settlements/s9", headers=H).json()["status"] == "pending"


def test_advance_fails_on_amount_mismatch_atomically() -> None:
    _make_paid_order("o10", 1000)
    _refund("sr10a", "o10", 300, "sk10a")
    _refund("sr10b", "o10", 200, "sk10b")
    _settle("s10", "o10", 999, ["sr10a", "sr10b"], "skey10")  # total is 500, not 999
    resp = _advance("s10", "akey10")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "amount_mismatch"
    assert client.get("/refunds/sr10a", headers=H).json()["status"] == "pending"
    assert client.get("/refunds/sr10b", headers=H).json()["status"] == "pending"
    assert client.get("/settlements/s10", headers=H).json()["status"] == "pending"


def test_advance_fails_when_refund_belongs_to_another_order() -> None:
    _make_paid_order("o11a", 1000)
    _make_paid_order("o11b", 1000)
    _refund("sr11a", "o11a", 100, "sk11a")
    _refund("sr11b", "o11b", 100, "sk11b")
    _settle("s11", "o11a", 200, ["sr11a", "sr11b"], "skey11")
    resp = _advance("s11", "akey11")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "refund_order_mismatch"
    assert client.get("/settlements/s11", headers=H).json()["status"] == "pending"


def test_only_one_settlement_may_write_off_a_shared_refund() -> None:
    _make_paid_order("o12", 1000)
    _refund("sr12", "o12", 400, "sk12")
    # two pending settlements both reference the same pending refund
    _settle("s12a", "o12", 400, ["sr12"], "skey12a")
    _settle("s12b", "o12", 400, ["sr12"], "skey12b")

    first = _advance("s12a", "akey12a")
    assert first.status_code == 200
    second = _advance("s12b", "akey12b")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "refund_already_settled"
    # loser changed nothing
    assert client.get("/settlements/s12b", headers=H).json()["status"] == "pending"
    assert client.get("/refunds/sr12", headers=H).json()["status"] == "effective"


def test_effective_settlement_cannot_advance_again() -> None:
    _make_paid_order("o13", 300)
    _refund("sr13", "o13", 300, "sk13")
    _settle("s13", "o13", 300, ["sr13"], "skey13")
    assert _advance("s13", "akey13a").status_code == 200
    again = _advance("s13", "akey13b")  # different request, same target
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "settlement_already_effective"
    assert client.get("/settlements/s13", headers=H).json()["status"] == "effective"
    # refund flipped exactly once
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='s13' AND operation='settle_advance' AND result_status=200").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


# ---------- quota conservation ----------

def test_refundable_conservation_after_advance() -> None:
    _make_paid_order("o14", 1000)
    _refund("sr14a", "o14", 300, "sk14a")
    _refund("sr14b", "o14", 200, "sk14b")
    _settle("s14", "o14", 500, ["sr14a", "sr14b"], "skey14")
    # pending refunds already occupy quota
    assert _refund("sr14c", "o14", 501, "sk14c").status_code == 409
    _advance("s14", "akey14")
    # after advancing the occupancy is unchanged (pending -> effective both occupy)
    assert _refund("sr14d", "o14", 501, "sk14d").status_code == 409
    ok = _refund("sr14e", "o14", 500, "sk14e")
    assert ok.status_code == 201

    conn = connect()
    try:
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE order_id='o14'").fetchone()
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM refunds "
            "WHERE order_id='o14' AND status IN ('pending','effective')").fetchone()["s"]
    finally:
        conn.close()
    assert occupied == 1000
    assert order["paid_cents"] - occupied == 0
    assert occupied <= order["paid_cents"] <= order["amount_cents"]


# ---------- idempotent replay ----------

def test_advance_replay_returns_first_success_and_flips_once() -> None:
    _make_paid_order("o15", 400)
    _refund("sr15", "o15", 400, "sk15")
    _settle("s15", "o15", 400, ["sr15"], "skey15")
    a = _advance("s15", "same-advance-key")
    b = _advance("s15", "same-advance-key")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='s15' AND operation='settle_advance'").fetchone()["c"]
        status = conn.execute("SELECT status FROM refunds WHERE refund_id='sr15'").fetchone()["status"]
    finally:
        conn.close()
    assert n == 1 and status == "effective"


def test_advance_failed_result_is_replayed_identically() -> None:
    _make_paid_order("o16", 1000)
    _refund("sr16", "o16", 100, "sk16")
    _settle("s16", "o16", 250, ["sr16"], "skey16")  # mismatch: referenced total is 100
    first = _advance("s16", "same-failing-key")
    assert first.status_code == 400
    assert first.json()["detail"]["code"] == "amount_mismatch"
    # state changes (the refund is reversed), yet the replay must return the
    # originally recorded failure, not the new "reversed" conflict
    client.post("/refunds/sr16/reverse", headers={**H, "Idempotency-Key": "rev16"})
    replay = _advance("s16", "same-failing-key")
    assert replay.status_code == 400
    assert replay.json() == first.json()
    assert client.get("/settlements/s16", headers=H).json()["status"] == "pending"
    # a different fingerprint evaluates current state: the refund is now reversed
    other = _advance("s16", "other-key")
    assert other.status_code == 409 and other.json()["detail"]["code"] == "refund_reversed"


def test_accept_replay_returns_first_result() -> None:
    _make_paid_order("o17", 300)
    _refund("sr17", "o17", 300, "sk17")
    a = _settle("s17", "o17", 300, ["sr17"], "same-accept-key")
    b = _settle("s17", "o17", 300, ["sr17"], "same-accept-key")
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) c FROM settlements WHERE settlement_id='s17'").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1


def test_concurrent_advances_only_one_wins() -> None:
    _make_paid_order("o18", 500)
    _refund("sr18", "o18", 500, "sk18")
    _settle("s18a", "o18", 500, ["sr18"], "skey18a")
    _settle("s18b", "o18", 500, ["sr18"], "skey18b")
    codes: list[int] = []

    def run(target: str, key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_advance(target, key).status_code)

    barrier = threading.Barrier(2)
    threads = [
        threading.Thread(target=run, args=("s18a", "ck18a", barrier)),
        threading.Thread(target=run, args=("s18b", "ck18b", barrier)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200, 409]
    assert client.get("/refunds/sr18", headers=H).json()["status"] == "effective"
    pending = [sid for sid in ("s18a", "s18b")
               if client.get(f"/settlements/{sid}", headers=H).json()["status"] == "pending"]
    effective = [sid for sid in ("s18a", "s18b")
                 if client.get(f"/settlements/{sid}", headers=H).json()["status"] == "effective"]
    assert len(pending) == 1 and len(effective) == 1


def test_concurrent_advances_of_same_settlement_only_one_wins() -> None:
    _make_paid_order("o19", 600)
    _refund("sr19a", "o19", 400, "sk19a")
    _refund("sr19b", "o19", 200, "sk19b")
    _settle("s19", "o19", 600, ["sr19a", "sr19b"], "skey19")
    codes: list[int] = []

    def run(key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_advance("s19", key).status_code)

    barrier = threading.Barrier(4)
    threads = [
        threading.Thread(target=run, args=(f"ck19-{i}", barrier)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 3
    assert client.get("/settlements/s19", headers=H).json()["status"] == "effective"
    assert client.get("/refunds/sr19a", headers=H).json()["status"] == "effective"
    assert client.get("/refunds/sr19b", headers=H).json()["status"] == "effective"
    # exactly one successful advance recorded; losers wrote an error record each
    conn = connect()
    try:
        ok_n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='s19' AND operation='settle_advance' AND result_status=200").fetchone()["c"]
        conflict_n = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='s19' AND operation='settle_advance' AND result_status=409").fetchone()["c"]
    finally:
        conn.close()
    assert ok_n == 1 and conflict_n == 3


# ---------- durability across restart ----------

def test_advance_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart_settlement.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    setup = (
        "from app.store.db import migrate\n"
        "from app.store import orders, refunds, settlements\n"
        "migrate()\n"
        "orders.insert('st','so',1000,'CNY')\n"
        "orders.add_payment('st','so',1000)\n"
        "refunds.accept('st','srr1','so',200,'why','rk1')\n"
        "refunds.accept('st','srr2','so',100,'why','rk2')\n"
        "settlements.accept('st','ss','so',300,['srr1','srr2'],'why','persist-accept')\n"
    )
    advance = (
        "import json\n"
        "from app.store.db import migrate\n"
        "from app.store import settlements\n"
        "migrate()\n"
        "out = settlements.advance('st','ss','persist-advance')\n"
        "print(json.dumps({'status': out.status, 'body_status': out.body.get('status')}))\n"
    )
    setup_run = subprocess.run([sys.executable, "-c", setup], env=env,
                               cwd=ROOT, capture_output=True, text=True, check=False)
    assert setup_run.returncode == 0, setup_run.stderr
    first = subprocess.run([sys.executable, "-c", advance], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", advance], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a["status"] == b["status"] == 200
    assert a["body_status"] == b["body_status"] == "effective"

    # the replay must not have flipped anything twice: one effective settlement,
    # both refunds effective, and exactly one successful advance record.
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        s_status = conn.execute(
            "SELECT status FROM settlements WHERE settlement_id='ss'").fetchone()[0]
        refund_states = [r[0] for r in conn.execute(
            "SELECT status FROM refunds WHERE refund_id IN ('srr1','srr2') ORDER BY refund_id").fetchall()]
        n_advance = conn.execute(
            "SELECT COUNT(*) FROM idempotent_requests "
            "WHERE target_id='ss' AND operation='settle_advance'").fetchone()[0]
    finally:
        conn.close()
    assert s_status == "effective"
    assert refund_states == ["effective", "effective"]
    assert n_advance == 1


# ---------- existing behavior unchanged ----------

def test_store_module_symbols_are_self_contained() -> None:
    # sanity: advancing a settlement through the store layer directly returns Outcome
    assert hasattr(settlement_store, "Outcome")
    assert settlement_store.PENDING == "pending"
    assert settlement_store.EFFECTIVE == "effective"
