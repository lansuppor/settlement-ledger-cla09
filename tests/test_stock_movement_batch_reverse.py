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
TENANT = "smb"
H = {"X-Tenant": TENANT}


def _make_order(order_id: str, amount: int = 1000, paid: int | None = None,
                tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")
    if paid is not None:
        order_store.add_payment(tenant, order_id, paid)


def _accept(movement_id: str, order_id: str, key: str, direction: str = "in",
            quantity: int = 10, tenant: str = TENANT):
    return client.post(
        "/stock-movements",
        json={"movement_id": movement_id, "order_id": order_id, "direction": direction,
              "quantity": quantity},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _reverse(movement_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/stock-movements/{movement_id}/reverse",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _batch(batch_id: str, movement_ids: list[str], tenant: str = TENANT):
    return client.post("/stock-movements/batch-reverse",
                       json={"batch_id": batch_id, "movement_ids": movement_ids},
                       headers={"X-Tenant": tenant})


def _get(movement_id: str, tenant: str = TENANT):
    return client.get(f"/stock-movements/{movement_id}", headers={"X-Tenant": tenant})


def _events(movement_id: str, tenant: str = TENANT) -> list[dict]:
    resp = client.get(f"/stock-movements/{movement_id}/events",
                      headers={"X-Tenant": tenant})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _event_count(movement_id: str, tenant: str = TENANT) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM stock_movement_events WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()["c"]
    finally:
        conn.close()


def _batch_records(batch_id: str, tenant: str = TENANT) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE tenant=? AND operation='stock_movement_batch_reverse' AND target_id=?",
            (tenant, batch_id),
        ).fetchone()["c"]
    finally:
        conn.close()


def _net_quantity(order_id: str, tenant: str = TENANT) -> int:
    resp = client.get("/stock-movements/summary", headers={"X-Tenant": tenant},
                      params={"order_id": order_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"][0]["net_quantity"]


# ---------- happy path ----------

def test_batch_reverse_flips_all_atomically_and_returns_sorted_results() -> None:
    _make_order("smbo1")
    _make_order("smbo1b")
    accepted = {}
    for mid, oid, direction, qty in (("smb1-c", "smbo1", "in", 10),
                                     ("smb1-a", "smbo1b", "out", 4),
                                     ("smb1-b", "smbo1", "in", 7)):
        resp = _accept(mid, oid, f"smb1-acc-{mid}", direction=direction, quantity=qty)
        assert resp.status_code == 201
        accepted[mid] = resp.json()

    # deliberately unordered submission
    resp = _batch("smb1-batch", ["smb1-c", "smb1-a", "smb1-b"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_id"] == "smb1-batch"
    assert [item["movement_id"] for item in body["items"]] == ["smb1-a", "smb1-b", "smb1-c"]
    for item in body["items"]:
        original = accepted[item["movement_id"]]
        assert item["order_id"] == original["order_id"]
        assert item["direction"] == original["direction"]
        assert item["quantity"] == original["quantity"]
        assert item["created_at"] == original["created_at"]
        assert item["status"] == "reversed"
        # readable by id with the reversed status and original values
        assert _get(item["movement_id"]).json() == item
        # exactly one accept + one reverse event per movement
        trail = _events(item["movement_id"])
        assert [e["operation"] for e in trail] == ["accept", "reverse"]
        assert [e["seq"] for e in trail] == [1, 2]
        assert trail[1]["status"] == "reversed"
        assert trail[1]["direction"] == original["direction"]
        assert trail[1]["quantity"] == original["quantity"]

    # reversed movements no longer count toward per-order or monthly summaries
    assert _net_quantity("smbo1") == 0
    assert _net_quantity("smbo1b") == 0
    monthly = client.get("/stock-movements/monthly-summary",
                         headers=H, params={"order_id": "smbo1"})
    assert monthly.status_code == 200
    assert all(item["net_quantity"] == 0 for item in monthly.json()["items"])
    # conditional search still finds them with original values and reversed status
    found = client.get("/stock-movements", headers=H,
                       params={"order_id": "smbo1", "status": "reversed"})
    assert {i["movement_id"] for i in found.json()["items"]} == {"smb1-b", "smb1-c"}


# ---------- validation ----------

def test_batch_reverse_validation_errors_write_nothing() -> None:
    _make_order("smbo2")
    _accept("smb2-a", "smbo2", "smb2-acc-a")
    _accept("smb2-b", "smbo2", "smb2-acc-b")

    no_tenant = client.post("/stock-movements/batch-reverse",
                            json={"batch_id": "smb2-b1", "movement_ids": ["smb2-a"]})
    assert no_tenant.status_code == 400
    missing_batch = client.post("/stock-movements/batch-reverse",
                                json={"movement_ids": ["smb2-a"]}, headers=H)
    assert missing_batch.status_code == 422
    blank_batch = client.post("/stock-movements/batch-reverse",
                              json={"batch_id": "  ", "movement_ids": ["smb2-a"]}, headers=H)
    assert blank_batch.status_code == 400
    missing_ids = client.post("/stock-movements/batch-reverse",
                              json={"batch_id": "smb2-b2"}, headers=H)
    assert missing_ids.status_code == 422
    empty_set = _batch("smb2-b3", [])
    assert empty_set.status_code == 422
    blank_id = _batch("smb2-b4", ["smb2-a", ""])
    assert blank_id.status_code == 400
    duplicates = _batch("smb2-b5", ["smb2-a", "smb2-b", "smb2-a"])
    assert duplicates.status_code == 400

    # none of the rejected requests touched anything
    assert _get("smb2-a").json()["status"] == "accepted"
    assert _get("smb2-b").json()["status"] == "accepted"
    assert _event_count("smb2-a") == 1
    assert _event_count("smb2-b") == 1
    assert _net_quantity("smbo2") == 20
    for batch_id in ("smb2-b1", "smb2-b2", "smb2-b3", "smb2-b4", "smb2-b5"):
        assert _batch_records(batch_id) == 0


# ---------- not found / conflict ----------

def test_batch_with_missing_movement_is_404_and_changes_nothing() -> None:
    _make_order("smbo3")
    _accept("smb3-a", "smbo3", "smb3-acc-a", quantity=5)
    resp = _batch("smb3-batch", ["smb3-a", "smb3-ghost"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "stock movement not found"
    assert _get("smb3-a").json()["status"] == "accepted"
    assert _event_count("smb3-a") == 1
    assert _net_quantity("smbo3") == 5


def test_batch_with_cross_tenant_movement_is_404_without_leak() -> None:
    _make_order("smbo4")
    _accept("smb4-a", "smbo4", "smb4-acc-a", quantity=6)
    _make_order("smbo4-foreign", tenant="t2")
    _accept("smb4-foreign", "smbo4-foreign", "smb4-acc-f", tenant="t2")
    resp = _batch("smb4-batch", ["smb4-a", "smb4-foreign"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "stock movement not found"
    # neither side changed
    assert _get("smb4-a").json()["status"] == "accepted"
    assert _get("smb4-foreign", tenant="t2").json()["status"] == "accepted"
    assert _event_count("smb4-a") == 1
    assert _event_count("smb4-foreign", tenant="t2") == 1


def test_batch_with_already_reversed_movement_is_409_and_changes_nothing() -> None:
    _make_order("smbo5")
    _accept("smb5-a", "smbo5", "smb5-acc-a", quantity=3)
    _accept("smb5-b", "smbo5", "smb5-acc-b", quantity=8)
    assert _reverse("smb5-b", "smb5-rev-single").status_code == 200

    resp = _batch("smb5-batch", ["smb5-a", "smb5-b"])
    assert resp.status_code == 409
    assert resp.json()["detail"] == "stock movement already reversed"
    # the accepted member was not flipped and gained no event
    assert _get("smb5-a").json()["status"] == "accepted"
    assert _event_count("smb5-a") == 1
    assert _event_count("smb5-b") == 2  # only the single reverse
    assert _net_quantity("smbo5") == 3


def test_batch_not_found_takes_precedence_over_conflict() -> None:
    _make_order("smbo6")
    _accept("smb6-a", "smbo6", "smb6-acc-a")
    assert _reverse("smb6-a", "smb6-rev").status_code == 200
    resp = _batch("smb6-batch", ["smb6-a", "smb6-ghost"])
    assert resp.status_code == 404
    assert resp.json()["detail"] == "stock movement not found"


# ---------- idempotent replay by (tenant, batch id) ----------

def test_batch_replay_same_set_any_order_returns_first_result() -> None:
    _make_order("smbo7")
    _accept("smb7-a", "smbo7", "smb7-acc-a", quantity=2)
    _accept("smb7-b", "smbo7", "smb7-acc-b", direction="out", quantity=9)
    first = _batch("smb7-batch", ["smb7-a", "smb7-b"])
    assert first.status_code == 200

    replay = _batch("smb7-batch", ["smb7-b", "smb7-a"])  # order changed
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # no second reversal: one record, two events per movement, states unchanged
    assert _batch_records("smb7-batch") == 1
    assert _event_count("smb7-a") == 2
    assert _event_count("smb7-b") == 2
    assert _get("smb7-a").json()["status"] == "reversed"
    assert _get("smb7-b").json()["status"] == "reversed"


def test_batch_replay_returns_first_404_even_after_movement_appears() -> None:
    first = _batch("smb8-batch", ["smb8-a"])
    assert first.status_code == 404
    _make_order("smbo8")
    _accept("smb8-a", "smbo8", "smb8-acc-a", quantity=4)
    replay = _batch("smb8-batch", ["smb8-a"])
    assert replay.status_code == 404
    assert replay.json() == first.json()
    # the replayed 404 did not reverse the now-existing movement
    assert _get("smb8-a").json()["status"] == "accepted"
    # a retry after failure needs a new batch id
    retry = _batch("smb8-batch-2", ["smb8-a"])
    assert retry.status_code == 200
    assert _get("smb8-a").json()["status"] == "reversed"


def test_batch_replay_returns_first_409_identically() -> None:
    _make_order("smbo9")
    _accept("smb9-a", "smbo9", "smb9-acc-a")
    assert _reverse("smb9-a", "smb9-rev").status_code == 200
    first = _batch("smb9-batch", ["smb9-a"])
    assert first.status_code == 409
    replay = _batch("smb9-batch", ["smb9-a"])
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert _batch_records("smb9-batch") == 1
    assert _event_count("smb9-a") == 2


def test_same_batch_id_with_different_set_conflicts_and_keeps_first_record() -> None:
    _make_order("smbo10")
    _accept("smb10-a", "smbo10", "smb10-acc-a", quantity=1)
    _accept("smb10-b", "smbo10", "smb10-acc-b", quantity=2)
    first = _batch("smb10-batch", ["smb10-a"])
    assert first.status_code == 200

    different = _batch("smb10-batch", ["smb10-a", "smb10-b"])
    assert different.status_code == 409
    assert different.json()["detail"] == "batch id already used with a different movement set"
    # smb10-b was not reversed by the conflicting submission
    assert _get("smb10-b").json()["status"] == "accepted"
    assert _event_count("smb10-b") == 1
    # the first record still governs the original set
    replay = _batch("smb10-batch", ["smb10-a"])
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert _batch_records("smb10-batch") == 1


def test_batch_id_does_not_occupy_movement_ids_or_single_reverse_keys() -> None:
    _make_order("smbo11")
    _accept("smb11-a", "smbo11", "smb11-acc-a", quantity=5)
    # a movement id may equal an existing batch id
    _accept("smb11-batch", "smbo11", "smb11-acc-b", quantity=6)
    resp = _batch("smb11-batch", ["smb11-a"])
    assert resp.status_code == 200
    # the movement named like the batch is untouched
    assert _get("smb11-batch").json()["status"] == "accepted"

    # single reverse keeps its own idempotent behaviour next to batch records
    first = _reverse("smb11-batch", "smb11-single-rev")
    assert first.status_code == 200
    replay = _reverse("smb11-batch", "smb11-single-rev")
    assert replay.status_code == 200 and replay.json() == first.json()
    assert _event_count("smb11-batch") == 2


def test_batch_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-batch-reverse.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import json, sys\n"
        "from app.store.db import migrate\n"
        "from app.store import orders, stock_movements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('smb','sbro',1000,'CNY')\n"
        "    stock_movements.accept('smb','sbr-a','sbro','in',11,'sbr-acc-a')\n"
        "    stock_movements.accept('smb','sbr-b','sbro','out',3,'sbr-acc-b')\n"
        "out = stock_movements.reverse_batch('smb','sbr-batch',['sbr-b','sbr-a'])\n"
        "print(json.dumps({'http': out.status, 'body': out.body}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b and a["http"] == 200
    assert [i["movement_id"] for i in a["body"]["items"]] == ["sbr-a", "sbr-b"]


# ---------- concurrency ----------

def test_concurrent_batches_sharing_a_movement_reverse_it_once() -> None:
    _make_order("smbo12")
    for mid in ("smb12-a", "smb12-b", "smb12-c"):
        _accept(mid, "smbo12", f"smb12-acc-{mid}", quantity=10)

    results: list[tuple[str, int]] = []
    barrier = threading.Barrier(2)

    def run(batch_id: str, ids: list[str], barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _batch(batch_id, ids)
        results.append((batch_id, resp.status_code))

    threads = [
        threading.Thread(target=run, args=("smb12-b1", ["smb12-a", "smb12-b"], barrier)),
        threading.Thread(target=run, args=("smb12-b2", ["smb12-b", "smb12-c"], barrier)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(code for _, code in results)
    assert codes == [200, 409]
    # the shared movement was reversed exactly once
    assert _get("smb12-b").json()["status"] == "reversed"
    assert _event_count("smb12-b") == 2
    # the losing batch failed as a whole: its exclusive movement is untouched
    winner = next(batch_id for batch_id, code in results if code == 200)
    loser_exclusive = "smb12-c" if winner == "smb12-b1" else "smb12-a"
    assert _get(loser_exclusive).json()["status"] == "accepted"
    assert _event_count(loser_exclusive) == 1
    # exactly two movements reversed overall, net quantity reflects it
    assert _net_quantity("smbo12") == 10


def test_concurrent_batch_and_single_reverse_single_winner() -> None:
    _make_order("smbo13")
    _accept("smb13-a", "smbo13", "smb13-acc-a", quantity=7)

    results: list[int] = []
    barrier = threading.Barrier(2)

    def run_batch(barrier: threading.Barrier) -> None:
        barrier.wait()
        results.append(_batch("smb13-batch", ["smb13-a"]).status_code)

    def run_single(barrier: threading.Barrier) -> None:
        barrier.wait()
        results.append(_reverse("smb13-a", "smb13-single").status_code)

    threads = [threading.Thread(target=run_batch, args=(barrier,)),
               threading.Thread(target=run_single, args=(barrier,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409]
    assert _get("smb13-a").json()["status"] == "reversed"
    assert _event_count("smb13-a") == 2  # accept + exactly one reverse


def test_concurrent_writer_conflict_is_retryable_and_not_recorded() -> None:
    _make_order("smbo14")
    _accept("smb14-a", "smbo14", "smb14-acc-a", quantity=4)
    conn = connect()
    try:
        # hold the write lock as a concurrent writer would
        conn.execute("BEGIN IMMEDIATE")
        resp = _batch("smb14-batch", ["smb14-a"])
        assert resp.status_code == 409
        assert "concurrent write" in resp.json()["detail"]
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    # the busy conflict left nothing behind and did not occupy the batch id
    assert _get("smb14-a").json()["status"] == "accepted"
    assert _event_count("smb14-a") == 1
    assert _batch_records("smb14-batch") == 0
    retry = _batch("smb14-batch", ["smb14-a"])
    assert retry.status_code == 200
    assert _get("smb14-a").json()["status"] == "reversed"


# ---------- isolation from the rest of the ledger ----------

def test_batch_reverse_never_mutates_other_documents() -> None:
    _make_order("smbo15", 1000, paid=800)
    client.post("/refunds",
                json={"refund_id": "smb15-r1", "order_id": "smbo15",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "smb15-r1"})
    client.post("/reconciliations",
                json={"batch_id": "smb15-b1", "order_id": "smbo15", "note": "月度对账"},
                headers={**H, "Idempotency-Key": "smb15-rec"})
    client.post("/tickets",
                json={"ticket_id": "smb15-w1", "order_id": "smbo15", "issue": "异常"},
                headers={**H, "Idempotency-Key": "smb15-tkt"})
    _accept("smb15-m1", "smbo15", "smb15-acc-m1", direction="in", quantity=50)
    _accept("smb15-m2", "smbo15", "smb15-acc-m2", direction="out", quantity=30)

    before = {
        "order": client.get("/orders/smbo15", headers=H).json(),
        "refund": client.get("/refunds/smb15-r1", headers=H).json(),
        "batch": client.get("/reconciliations/smb15-b1", headers=H).json(),
        "ticket": client.get("/tickets/smb15-w1", headers=H).json(),
    }
    resp = _batch("smb15-batch", ["smb15-m1", "smb15-m2"])
    assert resp.status_code == 200
    after = {
        "order": client.get("/orders/smbo15", headers=H).json(),
        "refund": client.get("/refunds/smb15-r1", headers=H).json(),
        "batch": client.get("/reconciliations/smb15-b1", headers=H).json(),
        "ticket": client.get("/tickets/smb15-w1", headers=H).json(),
    }
    assert before == after
