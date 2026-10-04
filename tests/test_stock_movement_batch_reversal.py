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
from app.store import stock_movements
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
# Dedicated tenant so this module's fixtures never collide with the other suites.
TENANT = "smb"
H = {"X-Tenant": TENANT}

MOVEMENT_FIELDS = {"movement_id", "order_id", "direction", "quantity", "status", "created_at"}


def _make_order(order_id: str, tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, 1000, "CNY")


def _accept(movement_id: str, order_id: str, key: str, direction: str = "in",
            quantity: int = 10, tenant: str = TENANT):
    return client.post(
        "/stock-movements",
        json={"movement_id": movement_id, "order_id": order_id, "direction": direction,
              "quantity": quantity},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _batch(batch_id: str, ids: list[str], tenant: str = TENANT, raw: dict | None = None):
    body = raw if raw is not None else {"batch_id": batch_id, "movement_ids": ids}
    return client.post("/stock-movements/batch-reversals", json=body,
                       headers={"X-Tenant": tenant})


def _get(movement_id: str, tenant: str = TENANT):
    return client.get(f"/stock-movements/{movement_id}", headers={"X-Tenant": tenant})


def _events(movement_id: str, tenant: str = TENANT):
    return client.get(f"/stock-movements/{movement_id}/events",
                      headers={"X-Tenant": tenant})


def _reverse_single(movement_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/stock-movements/{movement_id}/reverse",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _statuses(ids: list[str], tenant: str = TENANT) -> dict:
    conn = connect()
    try:
        return {r["movement_id"]: r["status"] for r in conn.execute(
            "SELECT movement_id, status FROM stock_movements "
            f"WHERE tenant=? AND movement_id IN ({','.join('?' for _ in ids)})",
            (tenant, *ids),
        ).fetchall()}
    finally:
        conn.close()


def _batch_row_count(batch_id: str, tenant: str = TENANT) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM stock_movement_reversal_batches "
            "WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()["c"]
    finally:
        conn.close()


# ---------- success ----------

def test_batch_reverses_all_in_one_transaction_and_sorts_results() -> None:
    _make_order("smbo1")
    accepted = {}
    for mid, d, q in (("smb1-c", "out", 30), ("smb1-a", "in", 10), ("smb1-b", "in", 20)):
        resp = _accept(mid, "smbo1", f"smb1-{mid}", direction=d, quantity=q)
        assert resp.status_code == 201
        accepted[mid] = resp.json()

    # deliberately unordered on the wire; the result is sorted by movement id
    resp = _batch("batch1", ["smb1-c", "smb1-a", "smb1-b"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"batch_id", "results"}
    assert body["batch_id"] == "batch1"
    assert [r["movement_id"] for r in body["results"]] == ["smb1-a", "smb1-b", "smb1-c"]
    for result in body["results"]:
        assert set(result) == MOVEMENT_FIELDS
        assert result["status"] == "reversed"
        first = accepted[result["movement_id"]]
        assert result["direction"] == first["direction"]
        assert result["quantity"] == first["quantity"]
        assert result["created_at"] == first["created_at"]

    # reads and the audit trail agree: exactly one accept + one reverse each
    for mid in accepted:
        got = _get(mid)
        assert got.status_code == 200 and got.json()["status"] == "reversed"
        trail = _events(mid).json()
        assert [e["operation"] for e in trail] == ["accept", "reverse"]
        assert trail[1]["direction"] == accepted[mid]["direction"]
        assert trail[1]["quantity"] == accepted[mid]["quantity"]
    assert _statuses(list(accepted)) == {mid: "reversed" for mid in accepted}


def test_batch_does_not_require_idempotency_key_header() -> None:
    _make_order("smbo2")
    _accept("smb2-a", "smbo2", "smb2-a")
    resp = client.post(
        "/stock-movements/batch-reversals",
        json={"batch_id": "batch2", "movement_ids": ["smb2-a"]},
        headers=H,
    )
    assert resp.status_code == 200, resp.text


# ---------- parameter validation ----------

def test_missing_tenant_batch_id_empty_set_and_duplicates_are_400_without_writes() -> None:
    _make_order("smbo3")
    _accept("smb3-a", "smbo3", "smb3-a")
    _accept("smb3-b", "smbo3", "smb3-b", direction="out", quantity=5)
    before = _statuses(["smb3-a", "smb3-b"])

    no_tenant = client.post("/stock-movements/batch-reversals",
                            json={"batch_id": "b", "movement_ids": ["smb3-a"]})
    assert no_tenant.status_code == 400

    for bad in (
        {"movement_ids": ["smb3-a"]},                       # batch id missing
        {"batch_id": "  ", "movement_ids": ["smb3-a"]},     # blank batch id
        {"batch_id": "b3"},                                  # id set missing
        {"batch_id": "b3", "movement_ids": []},             # empty set
        {"batch_id": "b3", "movement_ids": [""]},           # blank id
        {"batch_id": "b3", "movement_ids": ["smb3-a", "smb3-a"]},  # duplicate
    ):
        resp = _batch("b3", [], raw=bad)
        assert resp.status_code == 400, bad

    # no business writes, no batch record consumed
    assert _statuses(["smb3-a", "smb3-b"]) == before
    assert _batch_row_count("b3") == 0


# ---------- 404 / 409 precedence ----------

def test_missing_movement_makes_whole_batch_404_without_change() -> None:
    _make_order("smbo4")
    _accept("smb4-a", "smbo4", "smb4-a")
    resp = _batch("batch4", ["smb4-a", "smb4-ghost"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "stock movement not found"
    assert _get("smb4-a").json()["status"] == "accepted"
    assert len(_events("smb4-a").json()) == 1


def test_cross_tenant_movement_is_404_without_existence_leak() -> None:
    _make_order("smbo5")
    _accept("smb5-a", "smbo5", "smb5-a")
    _make_order("smbo5t", tenant="t2")
    _accept("smb5-foreign", "smbo5t", "smb5-foreign", tenant="t2")
    resp = _batch("batch5", ["smb5-a", "smb5-foreign"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "stock movement not found"
    # neither tenant's documents changed
    assert _get("smb5-a").json()["status"] == "accepted"
    assert _get("smb5-foreign", tenant="t2").json()["status"] == "accepted"


def test_any_reversed_movement_makes_whole_batch_409_without_change() -> None:
    _make_order("smbo6")
    _accept("smb6-a", "smbo6", "smb6-a")
    _accept("smb6-b", "smbo6", "smb6-b")
    assert _reverse_single("smb6-a", "smb6-a-rev").status_code == 200

    resp = _batch("batch6", ["smb6-a", "smb6-b"])
    assert resp.status_code == 409
    assert resp.json()["detail"] == "stock movement already reversed"
    assert _statuses(["smb6-a", "smb6-b"]) == {"smb6-a": "reversed", "smb6-b": "accepted"}
    assert len(_events("smb6-b").json()) == 1  # no reverse event appended


def test_404_takes_precedence_over_409() -> None:
    _make_order("smbo7")
    _accept("smb7-a", "smbo7", "smb7-a")
    assert _reverse_single("smb7-a", "smb7-a-rev").status_code == 200
    # both problems present: a reversed movement and a missing one
    resp = _batch("batch7", ["smb7-a", "smb7-ghost"])
    assert resp.status_code == 404


# ---------- batch replay semantics ----------

def test_same_batch_same_set_replays_first_success_even_after_state_changes() -> None:
    _make_order("smbo8")
    _accept("smb8-a", "smbo8", "smb8-a")
    first = _batch("batch8", ["smb8-a"])
    assert first.status_code == 200
    replay = _batch("batch8", ["smb8-a"])
    assert replay.status_code == 200 and replay.json() == first.json()
    # reordered submission is the same set
    replay_reordered = _batch("batch8", ["smb8-a"])
    assert replay_reordered.json() == first.json()

    # only one reverse event and one batch record ever
    assert len(_events("smb8-a").json()) == 2
    assert _batch_row_count("batch8") == 1


def test_recorded_404_is_replayed_identically_then_new_batch_succeeds() -> None:
    _make_order("smbo9")
    _accept("smb9-a", "smbo9", "smb9-a")
    first = _batch("batch9-404", ["smb9-a", "smb9-ghost"])
    assert first.status_code == 404
    # the missing movement later comes into existence — replay keeps first answer
    _accept("smb9-ghost", "smbo9", "smb9-ghost")
    replay = _batch("batch9-404", ["smb9-ghost", "smb9-a"])  # reordered too
    assert replay.status_code == 404 and replay.json() == first.json()
    assert _statuses(["smb9-a", "smb9-ghost"]) == {
        "smb9-a": "accepted", "smb9-ghost": "accepted"}

    # a fresh batch id can now do the work
    retry = _batch("batch9-retry", ["smb9-a", "smb9-ghost"])
    assert retry.status_code == 200
    assert [r["movement_id"] for r in retry.json()["results"]] == ["smb9-a", "smb9-ghost"]


def test_recorded_409_is_replayed_identically() -> None:
    _make_order("smbo10")
    _accept("smb10-a", "smbo10", "smb10-a")
    assert _reverse_single("smb10-a", "smb10-rev").status_code == 200
    early = _batch("batch10-409", ["smb10-a"])
    assert early.status_code == 409
    replay = _batch("batch10-409", ["smb10-a"])
    assert replay.status_code == 409 and replay.json() == early.json()
    # no extra reverse trail from the batch attempts
    assert len(_events("smb10-a").json()) == 2


def test_same_batch_different_set_conflicts_and_keeps_first_record() -> None:
    _make_order("smbo11")
    _accept("smb11-a", "smbo11", "smb11-a")
    _accept("smb11-b", "smbo11", "smb11-b")
    first = _batch("batch11", ["smb11-a"])
    assert first.status_code == 200

    mismatch = _batch("batch11", ["smb11-b"])
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == (
        "reversal batch was already submitted with a different movement set")
    mismatch2 = _batch("batch11", ["smb11-a", "smb11-b"])
    assert mismatch2.status_code == 409
    # first record intact and still replays; smb11-b was never reversed
    assert _batch("batch11", ["smb11-a"]).json() == first.json()
    assert _get("smb11-b").json()["status"] == "accepted"
    assert _batch_row_count("batch11") == 1


def test_batch_record_survives_process_restart() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "restart-batch.sqlite")
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    setup = (
        "from app.store.db import migrate\n"
        "from app.store import orders, stock_movements\n"
        "migrate()\n"
        "orders.insert('smb','sbo',1000,'CNY')\n"
        "stock_movements.accept('smb','sba','sbo','in',42,'sba-key')\n"
    )
    subprocess.run([sys.executable, "-c", setup], env=env, cwd=ROOT,
                   capture_output=True, text=True, check=True)
    script = (
        "import json\n"
        "from app.store.db import migrate\n"
        "from app.store import stock_movements\n"
        "migrate()\n"
        "out = stock_movements.batch_reverse('smb','sbatch',['sba'])\n"
        "print(json.dumps({'http': out.status, 'body': out.body, 'detail': out.detail}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                           capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                            capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b and a["http"] == 200
    assert a["body"]["results"][0]["status"] == "reversed"


# ---------- concurrency ----------

def test_concurrent_batches_with_the_same_documents_only_one_takes_effect() -> None:
    _make_order("smbo12")
    _accept("smb12-a", "smbo12", "smb12-a")
    _accept("smb12-b", "smbo12", "smb12-b")
    codes: list[int] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def run(i: int) -> None:
        barrier.wait()
        resp = _batch(f"batch12-{i}", ["smb12-a", "smb12-b"])
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # writers serialize: exactly one batch flips the pair, the rest answer a
    # business 409 (or a non-consuming 503 if they could not even start)
    assert sorted(codes).count(200) == 1, sorted(codes)
    assert all(c in (409, 503) for c in codes if c != 200), sorted(codes)
    assert _statuses(["smb12-a", "smb12-b"]) == {
        "smb12-a": "reversed", "smb12-b": "reversed"}
    assert len(_events("smb12-a").json()) == 2
    assert len(_events("smb12-b").json()) == 2

    # replay after the dust settles: the winning batch keeps its 200 and every
    # batch that recorded a first result replays it (409 losers stay 409; a
    # batch that only saw 503 now starts fresh and records 409)
    replay_codes = [_batch(f"batch12-{i}", ["smb12-a", "smb12-b"]).status_code
                    for i in range(8)]
    assert replay_codes.count(200) == 1
    assert all(c in (200, 409) for c in replay_codes)


def test_batch_racing_single_reversals_reverses_each_document_at_most_once() -> None:
    _make_order("smbo12b")
    _accept("smb12b-a", "smbo12b", "smb12b-a")
    _accept("smb12b-b", "smbo12b", "smb12b-b")
    outcomes: list[tuple[str, int]] = []
    barrier = threading.Barrier(3)
    lock = threading.Lock()

    def run(kind: str) -> None:
        barrier.wait()
        if kind == "batch":
            resp = _batch("batch12b", ["smb12b-a", "smb12b-b"])
        else:
            resp = _reverse_single(f"smb12b-{kind}", f"smb12b-{kind}-rev")
        with lock:
            outcomes.append((kind, resp.status_code))

    threads = [threading.Thread(target=run, args=(kind,)) for kind in ("batch", "a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # either the batch won (one 200, both singles lose) or the two singles won
    # (two 200s, the batch loses); in every case each doc reversed exactly once
    winners = [kind for kind, code in outcomes if code == 200]
    assert winners in (["batch"], ["a", "b"], ["b", "a"]), outcomes
    batch_code = next(code for kind, code in outcomes if kind == "batch")
    assert _statuses(["smb12b-a", "smb12b-b"]) == {
        "smb12b-a": "reversed", "smb12b-b": "reversed"}
    assert len(_events("smb12b-a").json()) == 2
    assert len(_events("smb12b-b").json()) == 2
    # the batch durably replays its own first result
    assert _batch("batch12b", ["smb12b-a", "smb12b-b"]).status_code == batch_code


def test_failed_batch_leaves_no_partial_state_or_events() -> None:
    _make_order("smbo13")
    ids = [f"smb13-{n}" for n in "abc"]
    for mid in ids:
        _accept(mid, "smbo13", f"smb13-{mid}")
    # one member missing -> the two existing members must be untouched
    resp = _batch("batch13", [*ids, "smb13-missing"])
    assert resp.status_code == 404
    assert _statuses(ids) == {mid: "accepted" for mid in ids}
    for mid in ids:
        assert [e["operation"] for e in _events(mid).json()] == ["accept"]


def test_single_reverse_and_batch_share_the_one_real_reversal() -> None:
    _make_order("smbo14")
    _accept("smb14-a", "smbo14", "smb14-a")
    _accept("smb14-b", "smbo14", "smb14-b")

    # single reversal wins first
    assert _reverse_single("smb14-a", "smb14-single").status_code == 200
    resp = _batch("batch14", ["smb14-a", "smb14-b"])
    assert resp.status_code == 409
    assert _get("smb14-b").json()["status"] == "accepted"
    # the failed batch id is consumed (its first 409 replays); a new batch works
    assert _batch("batch14", ["smb14-a", "smb14-b"]).status_code == 409
    ok = _batch("batch14-new", ["smb14-b"])
    assert ok.status_code == 200
    assert len(_events("smb14-a").json()) == 2
    assert len(_events("smb14-b").json()) == 2


# ---------- summaries no longer count reversed movements ----------

def test_batch_reversed_movements_drop_out_of_summaries_but_remain_queryable() -> None:
    _make_order("smbo15")
    _accept("smb15-in1", "smbo15", "smb15-in1", direction="in", quantity=100)
    _accept("smb15-out1", "smbo15", "smb15-out1", direction="out", quantity=40)
    _accept("smb15-in2", "smbo15", "smb15-in2", direction="in", quantity=5)
    assert _batch("batch15", ["smb15-in1", "smb15-out1"]).status_code == 200

    net = client.get("/stock-movements/summary?order_id=smbo15", headers=H)
    assert net.status_code == 200 and net.json()["items"][0]["net_quantity"] == 5
    monthly = client.get("/stock-movements/monthly-summary?order_id=smbo15", headers=H)
    assert monthly.status_code == 200 and monthly.json()["items"][0]["net_quantity"] == 5

    # by-id read keeps original values with reversed status
    got = _get("smb15-in1").json()
    assert got["quantity"] == 100 and got["direction"] == "in" and got["status"] == "reversed"
    # condition search still finds them when reversed are included
    listed = client.get("/stock-movements?order_id=smbo15&status=reversed", headers=H)
    assert {i["movement_id"] for i in listed.json()["items"]} == {"smb15-in1", "smb15-out1"}
    default = client.get("/stock-movements?order_id=smbo15", headers=H)
    assert [i["movement_id"] for i in default.json()["items"]] == ["smb15-in2"]


# ---------- retryable write contention ----------

def test_lock_contention_is_a_distinguishable_retryable_conflict_without_consuming_batch() -> None:
    # Set up the data first, then hold the writer lock from a second
    # connection; the store's BEGIN IMMEDIATE cannot start and must report 503
    # without inserting a batch record.
    _make_order("smbo16")
    _accept("smb16-a", "smbo16", "smb16-a")

    holder = connect()
    try:
        holder.execute("BEGIN IMMEDIATE")

        original = stock_movements.connect
        stock_movements.connect = lambda timeout=0.01: original(timeout=0.01)  # type: ignore[attr-defined]
        try:
            resp = _batch("batch16", ["smb16-a"])
        finally:
            stock_movements.connect = original  # type: ignore[attr-defined]
        assert resp.status_code == 503
        assert "contention" in resp.json()["detail"]
        assert _batch_row_count("batch16") == 0

        # batch id was not consumed: once the lock clears, the same batch runs
        holder.execute("ROLLBACK")
        retry = _batch("batch16", ["smb16-a"])
        assert retry.status_code == 200
        assert _get("smb16-a").json()["status"] == "reversed"
    finally:
        holder.close()


# ---------- isolation ----------

def test_batch_reversal_does_not_touch_other_documents() -> None:
    _make_order("smbo17", )
    order_store.add_payment(TENANT, "smbo17", 800)
    client.post("/refunds",
                json={"refund_id": "smb17-r1", "order_id": "smbo17",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "smb17-r1"})
    client.post("/settlements",
                json={"settlement_id": "smb17-s1", "order_id": "smbo17",
                      "amount_cents": 200, "refund_ids": ["smb17-r1"], "reason": "x"},
                headers={**H, "Idempotency-Key": "smb17-s1"})
    client.post("/reconciliations",
                json={"batch_id": "smb17-b1", "order_id": "smbo17", "note": "x"},
                headers={**H, "Idempotency-Key": "smb17-rec"})
    client.post("/tickets",
                json={"ticket_id": "smb17-w1", "order_id": "smbo17", "issue": "x"},
                headers={**H, "Idempotency-Key": "smb17-tkt"})
    _accept("smb17-m1", "smbo17", "smb17-m1")
    _accept("smb17-m2", "smbo17", "smb17-m2", direction="out", quantity=3)

    before = {
        "order": client.get("/orders/smbo17", headers=H).json(),
        "refund": client.get("/refunds/smb17-r1", headers=H).json(),
        "settlement": client.get("/settlements/smb17-s1", headers=H).json(),
        "batch": client.get("/reconciliations/smb17-b1", headers=H).json(),
        "ticket": client.get("/tickets/smb17-w1", headers=H).json(),
    }
    assert _batch("batch17", ["smb17-m1", "smb17-m2"]).status_code == 200
    after = {
        "order": client.get("/orders/smbo17", headers=H).json(),
        "refund": client.get("/refunds/smb17-r1", headers=H).json(),
        "settlement": client.get("/settlements/smb17-s1", headers=H).json(),
        "batch": client.get("/reconciliations/smb17-b1", headers=H).json(),
        "ticket": client.get("/tickets/smb17-w1", headers=H).json(),
    }
    assert before == after
