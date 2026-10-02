import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

# Dedicated tenant so this module's fixtures never collide with the other suites.
TENANT = "sms"
H = {"X-Tenant": TENANT}


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


def _reverse(movement_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/stock-movements/{movement_id}/reverse",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _summary(tenant: str = TENANT, **params):
    return client.get("/stock-movements/summary", headers={"X-Tenant": tenant},
                      params=params)


def _insert_direct(movement_id: str, order_id: str, direction: str, quantity: int,
                   created_at: str, tenant: str = TENANT, status: str = "accepted") -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO stock_movements(tenant, movement_id, order_id, direction, quantity, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, movement_id, order_id, direction, quantity, status, created_at),
        )
    finally:
        conn.close()


def _walk_pages(page_limit: int, tenant: str = TENANT, **params) -> list[dict]:
    items: list[dict] = []
    cursor = None
    seen_cursors = []
    while True:
        page_params = {**params, "limit": page_limit}
        if cursor is not None:
            page_params["cursor"] = cursor
        resp = _summary(tenant, **page_params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.append(cursor)
    return items


# ---------- aggregation ----------

def test_summary_aggregates_net_quantity_per_order() -> None:
    _make_order("smso1")
    _accept("sms1-in1", "smso1", "sms1-in1", direction="in", quantity=100)
    _accept("sms1-in2", "smso1", "sms1-in2", direction="in", quantity=50)
    _accept("sms1-out1", "smso1", "sms1-out1", direction="out", quantity=40)

    resp = _summary(order_id="smso1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [{"order_id": "smso1", "net_quantity": 110}],
                           "next_cursor": None}


def test_summary_counts_reversed_movements_as_zero_in_both_directions() -> None:
    _make_order("smso2")
    _accept("sms2-in", "smso2", "sms2-in", direction="in", quantity=70)
    _accept("sms2-out", "smso2", "sms2-out", direction="out", quantity=20)
    assert _reverse("sms2-in", "sms2-in-rev").status_code == 200
    assert _reverse("sms2-out", "sms2-out-rev").status_code == 200
    # both reversed -> net 0, but the order still has movements so it appears
    resp = _summary(order_id="smso2")
    assert resp.status_code == 200
    assert resp.json()["items"] == [{"order_id": "smso2", "net_quantity": 0}]

    _accept("sms2-in2", "smso2", "sms2-in2", direction="in", quantity=5)
    assert _summary(order_id="smso2").json()["items"][0]["net_quantity"] == 5


def test_summary_reflects_accept_reverse_and_replay_exactly() -> None:
    _make_order("smso3")
    _accept("sms3-in", "smso3", "sms3-in", direction="in", quantity=30)
    _accept("sms3-out", "smso3", "sms3-out", direction="out", quantity=12)
    assert _summary(order_id="smso3").json()["items"][0]["net_quantity"] == 18

    # replayed accept with the same fingerprint must not count twice
    again = _accept("sms3-in", "smso3", "sms3-in", direction="in", quantity=30)
    assert again.status_code == 201
    assert _summary(order_id="smso3").json()["items"][0]["net_quantity"] == 18

    assert _reverse("sms3-out", "sms3-out-rev").status_code == 200
    # replayed reverse with the same fingerprint must not flip twice
    assert _reverse("sms3-out", "sms3-out-rev").status_code == 200
    assert _summary(order_id="smso3").json()["items"][0]["net_quantity"] == 30


def test_summary_is_tenant_isolated() -> None:
    _make_order("smso4")
    _accept("sms4-in", "smso4", "sms4-in", direction="in", quantity=9)
    other = _summary("t2")
    assert other.status_code == 200
    assert other.json() == {"items": [], "next_cursor": None}
    assert _summary("t2", order_id="smso4").status_code == 400


# ---------- single-order lookup ----------

def test_summary_single_order_without_movements_is_400_without_leak() -> None:
    _make_order("smso5")  # exists but has no movements
    no_movements = _summary(order_id="smso5")
    ghost = _summary(order_id="sms-never-existed")
    assert no_movements.status_code == 400
    assert ghost.status_code == 400
    # indistinguishable: same status and same detail either way
    assert no_movements.json() == ghost.json()


# ---------- ordering & pagination ----------

def test_summary_orders_by_order_id_regardless_of_insertion() -> None:
    ts = "2026-09-01T00:00:00+00:00"
    for oid in ("smsp-c", "smsp-a", "smsp-b"):
        _make_order(oid)
    # insert deliberately out of order
    _insert_direct("smsp-3", "smsp-c", "in", 3, ts)
    _insert_direct("smsp-1", "smsp-a", "out", 1, ts)
    _insert_direct("smsp-2", "smsp-b", "in", 2, ts)

    first = _summary()
    ids = [i["order_id"] for i in first.json()["items"]
           if i["order_id"] in {"smsp-a", "smsp-b", "smsp-c"}]
    assert ids == ["smsp-a", "smsp-b", "smsp-c"]
    assert _summary().json() == first.json()  # stable across repeated queries


def test_summary_pagination_covers_everything_once_in_stable_order() -> None:
    ts = "2026-09-02T00:00:00+00:00"
    # dedicated tenant so this fixture's pages are exact
    for i in range(1, 8):
        _make_order(f"smsq-{i}", tenant="smsx")
        _insert_direct(f"smsq-x{i}", f"smsq-{i}", "in" if i % 2 else "out", i, ts,
                       tenant="smsx")

    expected = [{"order_id": f"smsq-{i}", "net_quantity": i if i % 2 else -i}
                for i in range(1, 8)]

    page1 = _summary("smsx", limit=3)
    assert page1.status_code == 200
    assert [i["order_id"] for i in page1.json()["items"]] == ["smsq-1", "smsq-2", "smsq-3"]
    assert page1.json()["next_cursor"] is not None

    page2 = _summary("smsx", limit=3, cursor=page1.json()["next_cursor"])
    assert [i["order_id"] for i in page2.json()["items"]] == ["smsq-4", "smsq-5", "smsq-6"]
    assert page2.json()["next_cursor"] is not None

    page3 = _summary("smsx", limit=3, cursor=page2.json()["next_cursor"])
    assert [i["order_id"] for i in page3.json()["items"]] == ["smsq-7"]
    assert page3.json()["next_cursor"] is None

    # page size exactly equal to total -> no cursor; one under -> cursor
    assert _summary("smsx", limit=7).json()["next_cursor"] is None
    assert _summary("smsx", limit=6).json()["next_cursor"] is not None

    # any page size walks the same full set in the same order, no dup/no miss
    for page_limit in (1, 2, 5, 100):
        assert _walk_pages(page_limit, "smsx") == expected

    # resuming from a middle cursor yields exactly the remaining tail
    tail = _walk_pages(3, "smsx")  # full walk sanity
    assert tail == expected
    mid = _summary("smsx", limit=2)
    rest = []
    cursor = mid.json()["next_cursor"]
    rest.extend(mid.json()["items"])
    while cursor is not None:
        page = _summary("smsx", limit=2, cursor=cursor).json()
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    assert rest == expected


def test_summary_empty_result_is_empty_list_without_cursor() -> None:
    resp = _summary("sms-empty-tenant")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


# ---------- validation ----------

def test_summary_invalid_params_are_rejected() -> None:
    assert client.get("/stock-movements/summary").status_code == 400
    assert _summary(limit=0).status_code == 422
    assert _summary(limit=501).status_code == 422
    assert _summary(limit="abc").status_code == 422
    bad = _summary(cursor="not-a-cursor")
    assert bad.status_code == 400
    assert bad.json()["detail"] == "cursor is invalid"


def test_summary_rejects_cursor_from_movement_search() -> None:
    _make_order("smsr1")
    _accept("smsr-m1", "smsr1", "smsr-m1", direction="in", quantity=1)
    _accept("smsr-m2", "smsr1", "smsr-m2", direction="in", quantity=1)
    search_page = client.get("/stock-movements", headers=H,
                             params={"order_id": "smsr1", "limit": 1})
    foreign_cursor = search_page.json()["next_cursor"]
    assert foreign_cursor is not None
    resp = _summary(cursor=foreign_cursor)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor is invalid"


# ---------- read-only isolation ----------

def test_summary_is_read_only_and_does_not_touch_other_documents() -> None:
    _make_order("smst1")
    client.post("/orders/smst1/payments", json={"amount_cents": 800}, headers=H)
    client.post("/refunds",
                json={"refund_id": "smst-r1", "order_id": "smst1",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "smst-r1"})
    client.post("/tickets",
                json={"ticket_id": "smst-w1", "order_id": "smst1", "issue": "异常"},
                headers={**H, "Idempotency-Key": "smst-w1"})
    _accept("smst-m1", "smst1", "smst-m1", direction="in", quantity=60)

    before = {
        "order": client.get("/orders/smst1", headers=H).json(),
        "refund": client.get("/refunds/smst-r1", headers=H).json(),
        "ticket": client.get("/tickets/smst-w1", headers=H).json(),
        "movement": client.get("/stock-movements/smst-m1", headers=H).json(),
    }
    assert _summary(order_id="smst1").status_code == 200
    assert _summary().status_code == 200
    after = {
        "order": client.get("/orders/smst1", headers=H).json(),
        "refund": client.get("/refunds/smst-r1", headers=H).json(),
        "ticket": client.get("/tickets/smst-w1", headers=H).json(),
        "movement": client.get("/stock-movements/smst-m1", headers=H).json(),
    }
    assert before == after


def test_summary_and_movement_search_are_independent() -> None:
    _make_order("smsu1")
    _accept("smsu-m1", "smsu1", "smsu-m1", direction="in", quantity=15)
    summary_body = _summary(order_id="smsu1").json()
    search_body = client.get("/stock-movements", headers=H,
                             params={"order_id": "smsu1"}).json()
    assert summary_body["items"] == [{"order_id": "smsu1", "net_quantity": 15}]
    assert [i["movement_id"] for i in search_body["items"]] == ["smsu-m1"]
    # repeating each query leaves the other's result unchanged
    assert _summary(order_id="smsu1").json() == summary_body
    assert client.get("/stock-movements", headers=H,
                      params={"order_id": "smsu1"}).json() == search_body
