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
TENANT = "smr"
H = {"X-Tenant": TENANT}

MOVEMENT_FIELDS = {"movement_id", "order_id", "direction", "quantity", "status", "created_at"}


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


def _get(movement_id: str, tenant: str = TENANT):
    return client.get(f"/stock-movements/{movement_id}", headers={"X-Tenant": tenant})


def _search(tenant: str = TENANT, **params):
    return client.get("/stock-movements", headers={"X-Tenant": tenant}, params=params)


def _net_quantity(order_id: str, tenant: str = TENANT) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='in' THEN quantity ELSE -quantity END),0) AS net "
            "FROM stock_movements WHERE tenant=? AND order_id=? AND status='accepted'",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    return row["net"]


# ---------- basic reverse ----------

def test_reverse_accepted_movement_returns_terminal_status() -> None:
    _make_order("sro1")
    accepted = _accept("sr1-a", "sro1", "sr1-acc", direction="in", quantity=12)
    assert accepted.status_code == 201 and accepted.json()["status"] == "accepted"

    resp = _reverse("sr1-a", "sr1-rev")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == MOVEMENT_FIELDS
    assert body["status"] == "reversed"
    # direction and quantity are preserved verbatim
    assert body["direction"] == "in" and body["quantity"] == 12
    assert body["created_at"] == accepted.json()["created_at"]

    got = _get("sr1-a")
    assert got.status_code == 200 and got.json() == body


def test_reverse_missing_or_cross_tenant_is_not_found_without_leak() -> None:
    assert _reverse("sr2-ghost", "sr2-rev").status_code == 404

    _make_order("sro2", tenant="t2")
    assert _accept("sr2-b", "sro2", "sr2-acc", tenant="t2").status_code == 201
    cross = _reverse("sr2-b", "sr2-rev-t1", tenant="t1")
    assert cross.status_code == 404
    # no existence leaked and no state changed across tenants
    assert _get("sr2-b", tenant="t2").json()["status"] == "accepted"
    assert _get("sr2-b", tenant="t1").status_code == 404


def test_double_reverse_conflicts_and_keeps_state() -> None:
    _make_order("sro3")
    assert _accept("sr3-a", "sro3", "sr3-acc").status_code == 201
    first = _reverse("sr3-a", "sr3-rev-a")
    assert first.status_code == 200
    again = _reverse("sr3-a", "sr3-rev-b")
    assert again.status_code == 409
    assert again.json()["detail"] == "stock movement already reversed"
    assert _get("sr3-a").json() == first.json()


def test_reverse_requires_headers() -> None:
    assert client.post("/stock-movements/sr1-a/reverse", headers=H).status_code == 400
    assert client.post("/stock-movements/sr1-a/reverse",
                       headers={"Idempotency-Key": "k"}).status_code == 400


# ---------- idempotent replay ----------

def test_reverse_replay_returns_first_result_and_flips_once() -> None:
    _make_order("sro4")
    assert _accept("sr4-a", "sro4", "sr4-acc").status_code == 201
    a = _reverse("sr4-a", "same-rev-key")
    b = _reverse("sr4-a", "same-rev-key")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()

    conn = connect()
    try:
        records = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE tenant=? AND target_id='sr4-a' AND operation='stock_movement_reverse'",
            (TENANT,),
        ).fetchone()["c"]
        reversed_count = conn.execute(
            "SELECT COUNT(*) c FROM stock_movements "
            "WHERE tenant=? AND movement_id='sr4-a' AND status='reversed'",
            (TENANT,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert records == 1 and reversed_count == 1


def test_failed_reverse_is_replayed_identically_even_when_state_changes() -> None:
    # first request targets a movement that does not exist yet
    first = _reverse("sr5-a", "sr5-rev")
    assert first.status_code == 404
    _make_order("sro5")
    assert _accept("sr5-a", "sro5", "sr5-acc").status_code == 201
    # the same fingerprint still replays the original 404 and changes nothing
    replay = _reverse("sr5-a", "sr5-rev")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert _get("sr5-a").json()["status"] == "accepted"
    # a different fingerprint evaluates current state and succeeds
    assert _reverse("sr5-a", "sr5-rev-retry").status_code == 200

    # a recorded 409 (double reverse) replays identically afterwards
    _make_order("sro5b")
    assert _accept("sr5-b", "sro5b", "sr5b-acc").status_code == 201
    assert _reverse("sr5-b", "sr5b-rev-a").status_code == 200
    conflict = _reverse("sr5-b", "sr5b-rev-b")
    assert conflict.status_code == 409
    replay2 = _reverse("sr5-b", "sr5b-rev-b")
    assert replay2.status_code == 409 and replay2.json() == conflict.json()
    assert _get("sr5-b").json()["status"] == "reversed"


def test_concurrent_reverses_distinct_keys_only_one_wins() -> None:
    _make_order("sro6")
    assert _accept("sr6-a", "sro6", "sr6-acc").status_code == 201
    codes: list[int] = []

    def run(key: str, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_reverse("sr6-a", key).status_code)

    barrier = threading.Barrier(6)
    threads = [threading.Thread(target=run, args=(f"sr6-rev-{i}", barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 5
    assert _get("sr6-a").json()["status"] == "reversed"


def test_reverse_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-stock-reverse.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import json, sys\n"
        "from app.store.db import connect, migrate\n"
        "from app.store import orders, stock_movements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('smr','sro',1000,'CNY')\n"
        "    stock_movements.accept('smr','srm','sro','out',7,'acc-key')\n"
        "out = stock_movements.reverse('smr','srm','rev-key')\n"
        "conn = connect()\n"
        "status = conn.execute(\"SELECT status FROM stock_movements WHERE movement_id='srm'\").fetchone()[0]\n"
        "conn.close()\n"
        "print(json.dumps({'http': out.status, 'status': status, 'body': out.body}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b and a["http"] == 200 and a["status"] == "reversed"
    assert a["body"]["status"] == "reversed" and a["body"]["quantity"] == 7


# ---------- read & search surface ----------

def test_search_hides_reversed_by_default_and_filters_by_status() -> None:
    _make_order("sro7")
    assert _accept("sr7-a", "sro7", "sr7-acc-a", direction="in", quantity=10).status_code == 201
    assert _accept("sr7-b", "sro7", "sr7-acc-b", direction="out", quantity=4).status_code == 201
    assert _accept("sr7-c", "sro7", "sr7-acc-c", direction="in", quantity=6).status_code == 201
    assert _reverse("sr7-b", "sr7-rev-b").status_code == 200

    def ids(**params) -> list[str]:
        resp = _search(**params)
        assert resp.status_code == 200, resp.text
        return [item["movement_id"] for item in resp.json()["items"]]

    # default: accepted only
    assert ids(order_id="sro7") == ["sr7-a", "sr7-c"]
    # exact status filter
    assert ids(order_id="sro7", status="reversed") == ["sr7-b"]
    assert ids(order_id="sro7", status="accepted") == ["sr7-a", "sr7-c"]
    # include_reversed returns everything, still in stable order
    assert ids(order_id="sro7", include_reversed=True) == ["sr7-a", "sr7-b", "sr7-c"]
    # status combines with the other filters as logical AND
    assert ids(order_id="sro7", direction="out", include_reversed=True) == ["sr7-b"]
    assert ids(order_id="sro7", direction="out") == []
    # every item carries the status field
    for item in _search(order_id="sro7", include_reversed=True).json()["items"]:
        assert set(item) == MOVEMENT_FIELDS


def test_search_status_and_include_reversed_together_is_400() -> None:
    resp = _search(status="accepted", include_reversed=True)
    assert resp.status_code == 400
    resp2 = _search(status="reversed", include_reversed="true")
    assert resp2.status_code == 400


def test_search_invalid_status_params_are_rejected() -> None:
    assert _search(status="pending").status_code == 422
    assert _search(include_reversed="not-a-bool").status_code == 422


def test_pagination_over_mixed_statuses_never_repeats_or_skips() -> None:
    _make_order("sro8")
    ts_ids = []
    for i in range(1, 8):
        mid = f"sr8-{i}"
        assert _accept(mid, "sro8", f"sr8-acc-{i}", direction="in" if i % 2 else "out",
                       quantity=i).status_code == 201
        ts_ids.append(mid)
    # reverse a few, including ones that will land mid-page
    for i, mid in enumerate(ts_ids):
        if i % 2 == 0:
            assert _reverse(mid, f"sr8-rev-{mid}").status_code == 200

    def walk(page_limit: int, **params) -> list[dict]:
        items: list[dict] = []
        cursor = None
        seen = []
        while True:
            page_params = {**params, "limit": page_limit}
            if cursor is not None:
                page_params["cursor"] = cursor
            resp = _search(**page_params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            items.extend(body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
            assert cursor not in seen
            seen.append(cursor)
        return items

    full = walk(3, order_id="sro8", include_reversed=True)
    assert [i["movement_id"] for i in full] == ts_ids
    for page_limit in (1, 2, 5, 100):
        assert [i["movement_id"] for i in walk(page_limit, order_id="sro8",
                                               include_reversed=True)] == ts_ids
    accepted_only = walk(2, order_id="sro8")
    assert [i["movement_id"] for i in accepted_only] == [m for i, m in enumerate(ts_ids) if i % 2 == 1]
    reversed_only = walk(2, order_id="sro8", status="reversed")
    assert [i["movement_id"] for i in reversed_only] == [m for i, m in enumerate(ts_ids) if i % 2 == 0]


# ---------- conservation ----------

def test_net_quantity_conservation_through_accept_reverse_replay() -> None:
    _make_order("sro9")
    assert _accept("sr9-in1", "sro9", "sr9-k1", direction="in", quantity=10).status_code == 201
    assert _accept("sr9-in2", "sro9", "sr9-k2", direction="in", quantity=5).status_code == 201
    assert _accept("sr9-out1", "sro9", "sr9-k3", direction="out", quantity=4).status_code == 201
    assert _net_quantity("sro9") == 10 + 5 - 4

    # reversing an inbound movement removes its quantity from the net
    assert _reverse("sr9-in1", "sr9-rev-1").status_code == 200
    assert _net_quantity("sro9") == 5 - 4
    # replay does not subtract twice
    assert _reverse("sr9-in1", "sr9-rev-1").status_code == 200
    assert _net_quantity("sro9") == 5 - 4
    # reversing the outbound movement adds its quantity back
    assert _reverse("sr9-out1", "sr9-rev-2").status_code == 200
    assert _net_quantity("sro9") == 5
    # a movement can never be reversed twice
    assert _reverse("sr9-out1", "sr9-rev-3").status_code == 409
    assert _net_quantity("sro9") == 5


# ---------- isolation from the rest of the ledger ----------

def test_reverse_never_mutates_other_documents() -> None:
    _make_order("sro10", 1000, paid=800)
    client.post("/refunds",
                json={"refund_id": "sr10-r1", "order_id": "sro10",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "sr10-r1"})
    client.post("/settlements",
                json={"settlement_id": "sr10-s1", "order_id": "sro10",
                      "amount_cents": 200, "refund_ids": ["sr10-r1"], "reason": "周期结算"},
                headers={**H, "Idempotency-Key": "sr10-s1-acc"})
    client.post("/settlements/sr10-s1/advance",
                headers={**H, "Idempotency-Key": "sr10-s1-adv"})
    client.post("/reconciliations",
                json={"batch_id": "sr10-b1", "order_id": "sro10", "note": "月度对账"},
                headers={**H, "Idempotency-Key": "sr10-rec"})
    client.post("/tickets",
                json={"ticket_id": "sr10-w1", "order_id": "sro10", "issue": "异常"},
                headers={**H, "Idempotency-Key": "sr10-tkt"})
    assert _accept("sr10-m1", "sro10", "sr10-m1-acc", direction="in", quantity=50).status_code == 201

    before = {
        "order": client.get("/orders/sro10", headers=H).json(),
        "refund": client.get("/refunds/sr10-r1", headers=H).json(),
        "settlement": client.get("/settlements/sr10-s1", headers=H).json(),
        "batch": client.get("/reconciliations/sr10-b1", headers=H).json(),
        "ticket": client.get("/tickets/sr10-w1", headers=H).json(),
    }
    assert _reverse("sr10-m1", "sr10-m1-rev").status_code == 200
    after = {
        "order": client.get("/orders/sro10", headers=H).json(),
        "refund": client.get("/refunds/sr10-r1", headers=H).json(),
        "settlement": client.get("/settlements/sr10-s1", headers=H).json(),
        "batch": client.get("/reconciliations/sr10-b1", headers=H).json(),
        "ticket": client.get("/tickets/sr10-w1", headers=H).json(),
    }
    assert before == after
    # refundable conservation is untouched: paid 800 - effective refund 200 = 600 left
    again = client.post("/refunds",
                        json={"refund_id": "sr10-r2", "order_id": "sro10",
                              "amount_cents": 600, "reason": "x"},
                        headers={**H, "Idempotency-Key": "sr10-r2"})
    assert again.status_code == 201
    over = client.post("/refunds",
                       json={"refund_id": "sr10-r3", "order_id": "sro10",
                             "amount_cents": 1, "reason": "x"},
                       headers={**H, "Idempotency-Key": "sr10-r3"})
    assert over.status_code == 409
