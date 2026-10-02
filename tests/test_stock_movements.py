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
TENANT = "sm"
H = {"X-Tenant": TENANT}

MOVEMENT_FIELDS = {"movement_id", "order_id", "direction", "quantity", "created_at"}


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


def _get(movement_id: str, tenant: str = TENANT):
    return client.get(f"/stock-movements/{movement_id}", headers={"X-Tenant": tenant})


def _search(tenant: str = TENANT, **params):
    return client.get("/stock-movements", headers={"X-Tenant": tenant}, params=params)


def _insert_direct(movement_id: str, order_id: str, direction: str, quantity: int,
                   created_at: str, tenant: str = TENANT) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO stock_movements(tenant, movement_id, order_id, direction, quantity, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (tenant, movement_id, order_id, direction, quantity, created_at),
        )
    finally:
        conn.close()


# ---------- accept & read ----------

def test_accept_movement_is_readable_and_keeps_values() -> None:
    _make_order("smo1")
    for direction, quantity, mid in (("in", 1, "sm1-in"), ("out", 999, "sm1-out")):
        resp = _accept(mid, "smo1", f"sm1-key-{direction}", direction=direction, quantity=quantity)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert set(body) == MOVEMENT_FIELDS
        assert body["movement_id"] == mid
        assert body["order_id"] == "smo1"
        assert body["direction"] == direction
        assert body["quantity"] == quantity
        assert isinstance(body["created_at"], str) and body["created_at"]

        got = _get(mid)
        assert got.status_code == 200 and got.json() == body


def test_accept_missing_or_cross_tenant_order_is_not_found_without_leak() -> None:
    resp = _accept("sm2-a", "no-such-order", "sm2-key", quantity=3)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "stock movement target not found"
    assert _get("sm2-a").status_code == 404

    _make_order("sm-foreign-o2", 500, tenant="t2")
    cross = _accept("sm2-b", "sm-foreign-o2", "sm2-key-2", tenant="t1")
    assert cross.status_code == 404
    assert _get("sm2-b", tenant="t1").status_code == 404


def test_read_movement_is_tenant_isolated() -> None:
    _make_order("smo3")
    assert _accept("sm3-a", "smo3", "sm3-key").status_code == 201
    assert _get("sm3-a", tenant="t2").status_code == 404
    assert client.get("/stock-movements/sm3-a").status_code == 400


def test_duplicate_movement_id_conflicts_and_keeps_first() -> None:
    _make_order("smo4")
    first = _accept("sm4-a", "smo4", "sm4-key-a", direction="in", quantity=10)
    assert first.status_code == 201
    dup = _accept("sm4-a", "smo4", "sm4-key-b", direction="out", quantity=20)
    assert dup.status_code == 409
    assert dup.json()["detail"] == "stock movement already accepted"
    # original direction/quantity are preserved verbatim
    assert _get("sm4-a").json() == first.json()


def test_invalid_arguments_are_rejected() -> None:
    _make_order("smo5")
    headers = {**H, "Idempotency-Key": "k"}

    no_tenant = client.post("/stock-movements",
                            json={"movement_id": "sm5-a", "order_id": "smo5",
                                  "direction": "in", "quantity": 1},
                            headers={"Idempotency-Key": "k"})
    assert no_tenant.status_code == 400
    no_key = client.post("/stock-movements",
                         json={"movement_id": "sm5-a", "order_id": "smo5",
                               "direction": "in", "quantity": 1}, headers=H)
    assert no_key.status_code == 400

    bad_direction = client.post("/stock-movements",
                                json={"movement_id": "sm5-a", "order_id": "smo5",
                                      "direction": "left", "quantity": 1}, headers=headers)
    assert bad_direction.status_code == 422
    for bad_quantity in (0, -1, 1.5, "3"):
        resp = client.post("/stock-movements",
                           json={"movement_id": "sm5-a", "order_id": "smo5",
                                 "direction": "in", "quantity": bad_quantity},
                           headers=headers)
        assert resp.status_code == 422, bad_quantity
    missing = client.post("/stock-movements",
                          json={"movement_id": "sm5-a", "order_id": "smo5"},
                          headers=headers)
    assert missing.status_code == 422
    assert _get("sm5-a").status_code == 404


# ---------- search: filtering ----------

def test_search_filters_combine_as_and_ranges_are_inclusive() -> None:
    _make_order("smo6a")
    _make_order("smo6b")
    t0, t1, t2 = "2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"
    rows = [
        ("sm6-1", "smo6a", "in", 10, t0),
        ("sm6-2", "smo6a", "out", 20, t1),
        ("sm6-3", "smo6a", "in", 30, t2),
        ("sm6-4", "smo6b", "out", 10, t1),
        ("sm6-5", "smo6b", "in", 40, t2),
    ]
    for row in rows:
        _insert_direct(*row)
    # noise in another tenant must never show up
    _make_order("smo6-other", tenant="t2")
    _insert_direct("sm6-x", "smo6-other", "in", 10, t1, tenant="t2")

    def ids(**params) -> list[str]:
        resp = _search(**params)
        assert resp.status_code == 200, resp.text
        return [item["movement_id"] for item in resp.json()["items"]]

    window = {"created_from": t0, "created_to": t2}
    assert ids(order_id="smo6a") == ["sm6-1", "sm6-2", "sm6-3"]
    assert ids(direction="in", **window) == ["sm6-1", "sm6-3", "sm6-5"]
    # inclusive endpoints; order is created_at ASC then id ASC
    assert ids(quantity_min=10, quantity_max=30, **window) == ["sm6-1", "sm6-2", "sm6-4", "sm6-3"]
    assert ids(created_from=t1, created_to=t2) == ["sm6-2", "sm6-4", "sm6-3", "sm6-5"]
    # arbitrary AND combination
    assert ids(order_id="smo6a", direction="in", quantity_min=10, quantity_max=30,
               created_from=t0, created_to=t1) == ["sm6-1"]
    assert ids(order_id="smo6b", direction="out", **window) == ["sm6-4"]
    assert ids(quantity_min=999, **window) == []


def test_search_empty_result_is_empty_list_without_cursor() -> None:
    _make_order("smo7")
    resp = _search(order_id="smo7")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


def test_search_invalid_params_are_400() -> None:
    assert _search(direction="sideways").status_code == 422
    assert _search(quantity_min=0).status_code == 422
    bad_range = _search(quantity_min=50, quantity_max=10)
    assert bad_range.status_code == 400
    assert client.get("/stock-movements").status_code == 400


# ---------- search: stable ordering ----------

def test_search_ordering_is_created_at_then_id_regardless_of_insertion() -> None:
    _make_order("smo8")
    ta, tb = "2026-04-01T00:00:00+00:00", "2026-04-02T00:00:00+00:00"
    # insert deliberately out of order, including a tie on created_at
    for mid, ts in (("sm8-z", tb), ("sm8-a", tb), ("sm8-m", ta), ("sm8-b", ta)):
        _insert_direct(mid, "smo8", "in", 5, ts)

    expected = ["sm8-b", "sm8-m", "sm8-a", "sm8-z"]
    first = _search(order_id="smo8")
    again = _search(order_id="smo8")
    assert [i["movement_id"] for i in first.json()["items"]] == expected
    assert first.json() == again.json()  # stable across repeated queries


# ---------- search: cursor pagination ----------

def _walk_pages(tenant: str, page_limit: int, **params) -> list[dict]:
    items: list[dict] = []
    cursor = None
    seen_cursors = []
    while True:
        page_params = {**params, "limit": page_limit}
        if cursor is not None:
            page_params["cursor"] = cursor
        resp = _search(tenant, **page_params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.append(cursor)
    return items


def test_pagination_covers_everything_once_in_stable_order() -> None:
    _make_order("smo9")
    ts = "2026-05-01T00:00:00+00:00"
    for i in range(1, 8):
        _insert_direct(f"sm9-{i}", "smo9", "in" if i % 2 else "out", i, ts)

    full = _walk_pages(TENANT, 3, order_id="smo9")
    assert [i["movement_id"] for i in full] == [f"sm9-{i}" for i in range(1, 8)]

    # first two pages promise more; the final page gives no cursor
    r1 = _search(order_id="smo9", limit=3)
    assert r1.json()["next_cursor"] is not None
    r2 = _search(order_id="smo9", limit=3, cursor=r1.json()["next_cursor"])
    assert [i["movement_id"] for i in r2.json()["items"]] == ["sm9-4", "sm9-5", "sm9-6"]
    assert r2.json()["next_cursor"] is not None
    r3 = _search(order_id="smo9", limit=3, cursor=r2.json()["next_cursor"])
    assert [i["movement_id"] for i in r3.json()["items"]] == ["sm9-7"]
    assert r3.json()["next_cursor"] is None

    # page size exactly equal to total -> no cursor; one over -> cursor
    assert _search(order_id="smo9", limit=7).json()["next_cursor"] is None
    assert _search(order_id="smo9", limit=6).json()["next_cursor"] is not None

    # different page sizes produce the same union, in the same order
    for page_limit in (1, 2, 5, 100):
        walked = _walk_pages(TENANT, page_limit, order_id="smo9")
        assert [i["movement_id"] for i in walked] == [f"sm9-{i}" for i in range(1, 8)]

    # resuming from a middle cursor always yields the matching tail
    tail = _search(order_id="smo9", limit=3, cursor=r1.json()["next_cursor"])
    rest = []
    cur = tail.json()["next_cursor"]
    rest.extend(tail.json()["items"])
    while cur is not None:
        page = _search(order_id="smo9", limit=3, cursor=cur).json()
        rest.extend(page["items"])
        cur = page["next_cursor"]
    assert [i["movement_id"] for i in rest] == [f"sm9-{i}" for i in range(4, 8)]


def test_pagination_interleaves_created_at_ties_across_pages() -> None:
    _make_order("smo10")
    ts = "2026-06-01T00:00:00+00:00"
    for name in ("a", "b", "c", "d", "e"):
        _insert_direct(f"sm10-{name}", "smo10", "in", 1, ts)
    walked = _walk_pages(TENANT, 2, order_id="smo10")
    assert [i["movement_id"] for i in walked] == [f"sm10-{n}" for n in "abcde"]


def test_invalid_cursor_is_rejected() -> None:
    resp = _search(cursor="not-a-cursor")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor is invalid"


# ---------- idempotent replay ----------

def test_replay_returns_first_result_and_accepts_once() -> None:
    _make_order("smo11")
    a = _accept("sm11-a", "smo11", "same-key", direction="in", quantity=11)
    b = _accept("sm11-a", "smo11", "same-key", direction="out", quantity=22)
    assert a.status_code == b.status_code == 201 and a.json() == b.json()

    conn = connect()
    try:
        records = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='sm11-a' AND operation='stock_movement_accept'").fetchone()["c"]
        movements = conn.execute(
            "SELECT COUNT(*) c FROM stock_movements WHERE movement_id='sm11-a'").fetchone()["c"]
    finally:
        conn.close()
    assert records == 1 and movements == 1


def test_failed_accept_is_replayed_identically_then_succeeds_with_new_key() -> None:
    first = _accept("sm12-a", "smo12-ghost", "sm12-key")
    assert first.status_code == 404
    _make_order("smo12-ghost", 500)
    replay = _accept("sm12-a", "smo12-ghost", "sm12-key")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert _accept("sm12-a", "smo12-ghost", "sm12-key-retry",
                   direction="out", quantity=7).status_code == 201


def test_concurrent_different_fingerprints_single_winner() -> None:
    _make_order("smo13")
    results: list[int] = []
    barrier = threading.Barrier(8)

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _accept("sm13-a", "smo13", f"sm13-key-{i}", direction="in", quantity=i + 1)
        results.append(resp.status_code)

    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 7

    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) c FROM stock_movements WHERE tenant=? AND movement_id='sm13-a'",
            (TENANT,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1


def test_accept_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-stock.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import json, sys\n"
        "from app.store.db import migrate\n"
        "from app.store import orders, stock_movements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('sm','so',1000,'CNY')\n"
        "out = stock_movements.accept('sm','sa','so','in',42,'acc-key')\n"
        "print(json.dumps({'http': out.status, 'body': out.body}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b and a["http"] == 201 and a["body"]["quantity"] == 42


# ---------- isolation from the rest of the ledger ----------

def test_movements_never_mutate_other_documents() -> None:
    _make_order("smo14", 1000, paid=800)
    client.post("/refunds",
                json={"refund_id": "sm14-r1", "order_id": "smo14",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "sm14-r1"})
    client.post("/settlements",
                json={"settlement_id": "sm14-s1", "order_id": "smo14",
                      "amount_cents": 200, "refund_ids": ["sm14-r1"], "reason": "周期结算"},
                headers={**H, "Idempotency-Key": "sm14-s1-acc"})
    client.post("/settlements/sm14-s1/advance",
                headers={**H, "Idempotency-Key": "sm14-s1-adv"})
    client.post("/reconciliations",
                json={"batch_id": "sm14-b1", "order_id": "smo14", "note": "月度对账"},
                headers={**H, "Idempotency-Key": "sm14-rec"})
    client.post("/tickets",
                json={"ticket_id": "sm14-w1", "order_id": "smo14", "issue": "异常"},
                headers={**H, "Idempotency-Key": "sm14-tkt"})

    before = {
        "order": client.get("/orders/smo14", headers=H).json(),
        "refund": client.get("/refunds/sm14-r1", headers=H).json(),
        "settlement": client.get("/settlements/sm14-s1", headers=H).json(),
        "batch": client.get("/reconciliations/sm14-b1", headers=H).json(),
        "ticket": client.get("/tickets/sm14-w1", headers=H).json(),
    }
    assert _accept("sm14-m1", "smo14", "sm14-m1-key", direction="in", quantity=50).status_code == 201
    assert _accept("sm14-m2", "smo14", "sm14-m2-key", direction="out", quantity=30).status_code == 201
    # reading must be side-effect free too
    assert _get("sm14-m1").status_code == 200
    assert _search(order_id="smo14").status_code == 200
    after = {
        "order": client.get("/orders/smo14", headers=H).json(),
        "refund": client.get("/refunds/sm14-r1", headers=H).json(),
        "settlement": client.get("/settlements/sm14-s1", headers=H).json(),
        "batch": client.get("/reconciliations/sm14-b1", headers=H).json(),
        "ticket": client.get("/tickets/sm14-w1", headers=H).json(),
    }
    assert before == after
    # refundable conservation: paid 800 - effective refund 200 = another 600 refundable
    again = client.post("/refunds",
                        json={"refund_id": "sm14-r2", "order_id": "smo14",
                              "amount_cents": 600, "reason": "x"},
                        headers={**H, "Idempotency-Key": "sm14-r2"})
    assert again.status_code == 201
    over = client.post("/refunds",
                       json={"refund_id": "sm14-r3", "order_id": "smo14",
                             "amount_cents": 1, "reason": "x"},
                       headers={**H, "Idempotency-Key": "sm14-r3"})
    assert over.status_code == 409
