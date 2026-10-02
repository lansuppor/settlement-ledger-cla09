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


def _make_order(order_id: str, amount: int = 1000, tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")


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
    return client.get("/stock-movements/summary", headers={"X-Tenant": tenant}, params=params)


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


def _expected_net(order_id: str, tenant: str = TENANT) -> int:
    """Independent per-movement tally: accepted in sum minus accepted out sum."""
    conn = connect()
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='in' AND status='accepted' THEN quantity ELSE 0 END),0) "
            "- COALESCE(SUM(CASE WHEN direction='out' AND status='accepted' THEN quantity ELSE 0 END),0) AS n "
            "FROM stock_movements WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["n"]
    finally:
        conn.close()


# ---------- aggregation ----------

def test_summary_nets_accepted_in_minus_accepted_out_per_order() -> None:
    _make_order("sso1a")
    _make_order("sso1b")
    _accept("ss1-1", "sso1a", "ss1-k1", direction="in", quantity=100)
    _accept("ss1-2", "sso1a", "ss1-k2", direction="in", quantity=50)
    _accept("ss1-3", "sso1a", "ss1-k3", direction="out", quantity=40)
    _accept("ss1-4", "sso1b", "ss1-k4", direction="out", quantity=7)

    resp = _summary(order_id="sso1a")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [{"order_id": "sso1a", "net_quantity": 110}],
                           "next_cursor": None}
    assert _summary(order_id="sso1b").json()["items"] == [
        {"order_id": "sso1b", "net_quantity": -7}]

    # the summary equals the per-movement tally for every order
    for oid in ("sso1a", "sso1b"):
        assert _summary(order_id=oid).json()["items"][0]["net_quantity"] == _expected_net(oid)


def test_summary_counts_reversed_movements_as_zero_regardless_of_direction() -> None:
    _make_order("sso2")
    _accept("ss2-in", "sso2", "ss2-k1", direction="in", quantity=30)
    _accept("ss2-out", "sso2", "ss2-k2", direction="out", quantity=12)
    assert _summary(order_id="sso2").json()["items"][0]["net_quantity"] == 18

    assert _reverse("ss2-in", "ss2-rev1").status_code == 200
    assert _summary(order_id="sso2").json()["items"][0]["net_quantity"] == -12
    assert _reverse("ss2-out", "ss2-rev2").status_code == 200
    assert _summary(order_id="sso2").json()["items"][0]["net_quantity"] == 0
    # the order still has movements, so it stays in the summary with net 0
    assert _summary(order_id="sso2").json()["items"] == [
        {"order_id": "sso2", "net_quantity": 0}]


def test_summary_is_tenant_isolated() -> None:
    _make_order("sso3", tenant="t2")
    _accept("ss3-a", "sso3", "ss3-k1", direction="in", quantity=9, tenant="t2")

    assert _summary(order_id="sso3").status_code == 400  # no movements under this tenant
    assert _summary(order_id="sso3", tenant="t2").json()["items"] == [
        {"order_id": "sso3", "net_quantity": 9}]
    assert all(item["order_id"] != "sso3" for item in _summary().json()["items"])


def test_summary_requires_tenant_header() -> None:
    assert client.get("/stock-movements/summary").status_code == 400


# ---------- single-order lookup ----------

def test_summary_single_order_without_movements_is_400_without_leak() -> None:
    _make_order("sso4-exists-no-movements")
    existing = _summary(order_id="sso4-exists-no-movements")
    ghost = _summary(order_id="sso4-ghost")
    # identical response whether the order exists or not: no existence leak
    assert existing.status_code == ghost.status_code == 400
    assert existing.json() == ghost.json()


def test_summary_single_order_ignores_pagination_params() -> None:
    _make_order("sso5")
    _accept("ss5-a", "sso5", "ss5-k1", direction="in", quantity=5)
    resp = _summary(order_id="sso5", limit=1)
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"order_id": "sso5", "net_quantity": 5}],
                           "next_cursor": None}


# ---------- all-orders listing: stable ordering ----------

def test_summary_orders_sort_by_order_id_regardless_of_insertion() -> None:
    ts = "2026-01-01T00:00:00+00:00"
    # insert deliberately out of order
    for oid, mid in (("sso6-z", "ss6-1"), ("sso6-a", "ss6-2"), ("sso6-m", "ss6-3")):
        _make_order(oid)
        _insert_direct(mid, oid, "in", 1, ts)

    resp = _summary(limit=500)
    assert resp.status_code == 200, resp.text
    order_ids = [item["order_id"] for item in resp.json()["items"]]
    assert order_ids == sorted(order_ids)
    assert order_ids.index("sso6-a") < order_ids.index("sso6-m") < order_ids.index("sso6-z")
    assert _summary(limit=500).json() == resp.json()  # stable across repeated queries


def test_summary_empty_result_is_empty_list_without_cursor() -> None:
    resp = _summary(tenant="sms-empty")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


# ---------- cursor pagination ----------

def _walk_summary_pages(tenant: str, page_limit: int) -> list[dict]:
    items: list[dict] = []
    cursor = None
    seen_cursors = []
    while True:
        params = {"limit": page_limit}
        if cursor is not None:
            params["cursor"] = cursor
        resp = _summary(tenant, **params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.append(cursor)
    return items


def test_summary_pagination_covers_everything_once_in_stable_order() -> None:
    tenant = "sms-page"
    ts = "2026-02-01T00:00:00+00:00"
    expected = []
    for i in range(1, 8):
        oid = f"ssp-{i:02d}"
        _make_order(oid, tenant=tenant)
        _insert_direct(f"ssp-m{i}", oid, "in" if i % 2 else "out", i, ts, tenant=tenant)
        expected.append({"order_id": oid, "net_quantity": i if i % 2 else -i})

    for page_limit in (1, 2, 3, 5, 100):
        assert _walk_summary_pages(tenant, page_limit) == expected

    # page size exactly equal to total -> no cursor; one under -> cursor
    assert _summary(tenant, limit=7).json()["next_cursor"] is None
    assert _summary(tenant, limit=6).json()["next_cursor"] is not None

    # resuming from a middle cursor yields exactly the matching tail
    first = _summary(tenant, limit=3).json()
    assert first["items"] == expected[:3]
    rest = []
    cursor = first["next_cursor"]
    while cursor is not None:
        page = _summary(tenant, limit=3, cursor=cursor).json()
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    assert rest == expected[3:]


def test_summary_invalid_cursor_is_rejected() -> None:
    resp = _summary(cursor="not-a-cursor")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor is invalid"
    # a cursor issued by the movement search endpoint is not valid here either
    _make_order("sso7")
    _accept("ss7-a", "sso7", "ss7-k1", direction="in", quantity=1)
    search_cursor = client.get("/stock-movements", headers=H,
                               params={"order_id": "sso7", "limit": 1}).json()
    assert search_cursor["next_cursor"] is None  # single row: no cursor issued
    _accept("ss7-b", "sso7", "ss7-k2", direction="in", quantity=1)
    search_cursor = client.get("/stock-movements", headers=H,
                               params={"order_id": "sso7", "limit": 1}).json()["next_cursor"]
    assert _summary(cursor=search_cursor).status_code == 400


def test_summary_invalid_limit_is_rejected() -> None:
    assert _summary(limit=0).status_code == 422
    assert _summary(limit=-1).status_code == 422
    assert _summary(limit=501).status_code == 422
    assert _summary(limit="abc").status_code == 422


# ---------- consistency with movement operations ----------

def test_summary_tracks_accept_reverse_and_replay_sequences() -> None:
    _make_order("sso8")
    _accept("ss8-in1", "sso8", "ss8-k1", direction="in", quantity=100)
    _accept("ss8-in2", "sso8", "ss8-k2", direction="in", quantity=50)
    _accept("ss8-out1", "sso8", "ss8-k3", direction="out", quantity=40)

    def net() -> int:
        return _summary(order_id="sso8").json()["items"][0]["net_quantity"]

    assert net() == 110 == _expected_net("sso8")

    # replayed accepts (same fingerprint) do not count twice
    _accept("ss8-in1", "sso8", "ss8-k1", direction="in", quantity=100)
    assert net() == 110

    assert _reverse("ss8-out1", "ss8-rev1").status_code == 200
    assert net() == 150 == _expected_net("sso8")

    # replayed reverses do not flip or count twice
    assert _reverse("ss8-out1", "ss8-rev1").status_code == 200
    assert _reverse("ss8-out1", "ss8-rev2").status_code == 409
    assert net() == 150 == _expected_net("sso8")


def test_summary_is_read_only_and_never_mutates_other_documents() -> None:
    _make_order("sso9", 1000)
    order_store.add_payment(TENANT, "sso9", 800)
    client.post("/refunds",
                json={"refund_id": "ss9-r1", "order_id": "sso9",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "ss9-r1"})
    client.post("/tickets",
                json={"ticket_id": "ss9-w1", "order_id": "sso9", "issue": "异常"},
                headers={**H, "Idempotency-Key": "ss9-tkt"})
    _accept("ss9-m1", "sso9", "ss9-m1", direction="in", quantity=60)

    before = {
        "order": client.get("/orders/sso9", headers=H).json(),
        "refund": client.get("/refunds/ss9-r1", headers=H).json(),
        "ticket": client.get("/tickets/ss9-w1", headers=H).json(),
        "movement": client.get("/stock-movements/ss9-m1", headers=H).json(),
    }
    assert _summary(order_id="sso9").status_code == 200
    assert _summary().status_code == 200
    after = {
        "order": client.get("/orders/sso9", headers=H).json(),
        "refund": client.get("/refunds/ss9-r1", headers=H).json(),
        "ticket": client.get("/tickets/ss9-w1", headers=H).json(),
        "movement": client.get("/stock-movements/ss9-m1", headers=H).json(),
    }
    assert before == after
