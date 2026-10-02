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
TENANT = "smm"
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


def _monthly(tenant: str = TENANT, **params):
    return client.get("/stock-movements/monthly-summary",
                      headers={"X-Tenant": tenant}, params=params)


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


def _expected_monthly_net(order_id: str, month: str, tenant: str = TENANT) -> int:
    """Independent per-movement tally for one (order, month)."""
    conn = connect()
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='in' AND status='accepted' THEN quantity ELSE 0 END),0) "
            "- COALESCE(SUM(CASE WHEN direction='out' AND status='accepted' THEN quantity ELSE 0 END),0) AS n "
            "FROM stock_movements WHERE tenant=? AND order_id=? AND substr(created_at,1,7)=?",
            (tenant, order_id, month),
        ).fetchone()["n"]
    finally:
        conn.close()


# ---------- aggregation ----------

def test_monthly_nets_accepted_in_minus_accepted_out_per_order_and_month() -> None:
    _make_order("smo1")
    _insert_direct("sm1-1", "smo1", "in", 100, "2026-01-05T08:00:00+00:00")
    _insert_direct("sm1-2", "smo1", "in", 50, "2026-01-20T23:59:59+00:00")
    _insert_direct("sm1-3", "smo1", "out", 40, "2026-01-31T00:00:00+00:00")
    _insert_direct("sm1-4", "smo1", "out", 7, "2026-02-01T00:00:00+00:00")

    resp = _monthly(order_id="smo1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "items": [
            {"order_id": "smo1", "month": "2026-01", "net_quantity": 110},
            {"order_id": "smo1", "month": "2026-02", "net_quantity": -7},
        ],
        "next_cursor": None,
    }
    for month in ("2026-01", "2026-02"):
        assert _expected_monthly_net("smo1", month) in (110, -7)


def test_monthly_counts_reversed_movements_as_zero_regardless_of_direction() -> None:
    _make_order("smo2")
    _accept("sm2-in", "smo2", "sm2-k1", direction="in", quantity=30)
    _accept("sm2-out", "smo2", "sm2-k2", direction="out", quantity=12)
    month = _monthly(order_id="smo2").json()["items"][0]["month"]
    assert _monthly(order_id="smo2").json()["items"] == [
        {"order_id": "smo2", "month": month, "net_quantity": 18}]

    assert _reverse("sm2-in", "sm2-rev1").status_code == 200
    assert _monthly(order_id="smo2").json()["items"][0]["net_quantity"] == -12
    assert _reverse("sm2-out", "sm2-rev2").status_code == 200
    # the order still has movements that month, so the row stays with net 0
    assert _monthly(order_id="smo2").json()["items"] == [
        {"order_id": "smo2", "month": month, "net_quantity": 0}]


def test_monthly_nets_sum_to_per_order_summary_net() -> None:
    _make_order("smo3")
    _insert_direct("sm3-1", "smo3", "in", 11, "2025-12-15T10:00:00+00:00")
    _insert_direct("sm3-2", "smo3", "out", 4, "2026-01-02T10:00:00+00:00")
    _insert_direct("sm3-3", "smo3", "in", 20, "2026-01-20T10:00:00+00:00")
    _insert_direct("sm3-4", "smo3", "out", 9, "2026-03-01T10:00:00+00:00",
                   status="reversed")

    monthly = _monthly(order_id="smo3").json()["items"]
    assert [(i["month"], i["net_quantity"]) for i in monthly] == [
        ("2025-12", 11), ("2026-01", 16), ("2026-03", 0)]
    summary = client.get("/stock-movements/summary", headers=H,
                         params={"order_id": "smo3"}).json()
    assert sum(i["net_quantity"] for i in monthly) == summary["items"][0]["net_quantity"]


def test_monthly_is_tenant_isolated() -> None:
    _make_order("smo4", tenant="t2")
    _insert_direct("sm4-a", "smo4", "in", 9, "2026-04-10T00:00:00+00:00", tenant="t2")

    assert _monthly(order_id="smo4").status_code == 400  # no movements under this tenant
    assert _monthly(order_id="smo4", tenant="t2").json()["items"] == [
        {"order_id": "smo4", "month": "2026-04", "net_quantity": 9}]
    assert all(item["order_id"] != "smo4" for item in _monthly(limit=500).json()["items"])


def test_monthly_requires_tenant_header() -> None:
    assert client.get("/stock-movements/monthly-summary").status_code == 400


# ---------- single-order lookup ----------

def test_monthly_single_order_without_movements_is_400_without_leak() -> None:
    _make_order("smo5-exists-no-movements")
    existing = _monthly(order_id="smo5-exists-no-movements")
    ghost = _monthly(order_id="smo5-ghost")
    # identical response whether the order exists or not: no existence leak
    assert existing.status_code == ghost.status_code == 400
    assert existing.json() == ghost.json()


def test_monthly_single_order_returns_each_month_once() -> None:
    _make_order("smo6")
    _insert_direct("sm6-1", "smo6", "in", 3, "2026-05-01T00:00:00+00:00")
    _insert_direct("sm6-2", "smo6", "in", 4, "2026-05-02T00:00:00+00:00")
    _insert_direct("sm6-3", "smo6", "out", 2, "2026-06-01T00:00:00+00:00")

    resp = _monthly(order_id="smo6")
    assert resp.status_code == 200
    assert resp.json() == {
        "items": [
            {"order_id": "smo6", "month": "2026-05", "net_quantity": 7},
            {"order_id": "smo6", "month": "2026-06", "net_quantity": -2},
        ],
        "next_cursor": None,
    }


# ---------- all-orders listing: stable ordering ----------

def test_monthly_sorts_by_order_then_month_regardless_of_insertion() -> None:
    # insert deliberately out of order
    for oid in ("smo7-z", "smo7-a"):
        _make_order(oid)
    rows = [
        ("sm7-1", "smo7-z", "in", 1, "2026-02-01T00:00:00+00:00"),
        ("sm7-2", "smo7-a", "in", 1, "2026-03-01T00:00:00+00:00"),
        ("sm7-3", "smo7-a", "in", 1, "2026-01-01T00:00:00+00:00"),
        ("sm7-4", "smo7-z", "in", 1, "2026-01-01T00:00:00+00:00"),
    ]
    for mid, oid, direction, qty, ts in rows:
        _insert_direct(mid, oid, direction, qty, ts)

    resp = _monthly(limit=500)
    assert resp.status_code == 200, resp.text
    keys = [(item["order_id"], item["month"]) for item in resp.json()["items"]]
    assert keys == sorted(keys)
    own = [k for k in keys if k[0].startswith("smo7-")]
    assert own == [("smo7-a", "2026-01"), ("smo7-a", "2026-03"),
                   ("smo7-z", "2026-01"), ("smo7-z", "2026-02")]
    assert _monthly(limit=500).json() == resp.json()  # stable across repeated queries


def test_monthly_empty_result_is_empty_list_without_cursor() -> None:
    resp = _monthly(tenant="smm-empty")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


# ---------- cursor pagination ----------

def _walk_monthly_pages(tenant: str, page_limit: int, **params) -> list[dict]:
    items: list[dict] = []
    cursor = None
    seen_cursors = set()
    while True:
        query = {"limit": page_limit, **params}
        if cursor is not None:
            query["cursor"] = cursor
        resp = _monthly(tenant, **query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.add(cursor)
    return items


def test_monthly_pagination_covers_everything_once_in_stable_order() -> None:
    tenant = "smm-page"
    expected = []
    plan = [
        ("smp-o1", "2026-01", 5), ("smp-o1", "2026-02", -2),
        ("smp-o2", "2026-01", 7), ("smp-o2", "2026-03", 1),
        ("smp-o3", "2025-12", 4), ("smp-o3", "2026-01", 6),
        ("smp-o3", "2026-04", -3),
    ]
    for oid in ("smp-o1", "smp-o2", "smp-o3"):
        order_store.insert(tenant, oid, 1000, "CNY")
    for index, (oid, month, net) in enumerate(plan):
        direction = "in" if net > 0 else "out"
        _insert_direct(f"smp-{index}", oid, direction, abs(net),
                       f"{month}-15T00:00:00+00:00", tenant=tenant)
        expected.append({"order_id": oid, "month": month, "net_quantity": net})
    expected.sort(key=lambda item: (item["order_id"], item["month"]))

    for page_limit in (1, 2, 3, 5, 100):
        assert _walk_monthly_pages(tenant, page_limit) == expected

    # page size exactly equal to total -> no cursor; one under -> cursor
    assert _monthly(tenant, limit=7).json()["next_cursor"] is None
    assert _monthly(tenant, limit=6).json()["next_cursor"] is not None

    # resuming from a middle cursor yields exactly the matching tail
    first = _monthly(tenant, limit=3).json()
    assert first["items"] == expected[:3]
    rest = []
    cursor = first["next_cursor"]
    while cursor is not None:
        page = _monthly(tenant, limit=3, cursor=cursor).json()
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    assert rest == expected[3:]

    # paginating a single order walks its months only
    own = _walk_monthly_pages(tenant, 2, order_id="smp-o3")
    assert own == [item for item in expected if item["order_id"] == "smp-o3"]


def test_monthly_invalid_cursor_is_rejected() -> None:
    resp = _monthly(cursor="not-a-cursor")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor is invalid"
    # a cursor issued by the per-order summary endpoint is not valid here
    _make_order("smo8")
    _accept("sm8-a", "smo8", "sm8-k1", direction="in", quantity=1)
    _make_order("smo9")
    _accept("sm9-a", "smo9", "sm9-k1", direction="in", quantity=1)
    summary_cursor = client.get("/stock-movements/summary", headers=H,
                                params={"limit": 1}).json()["next_cursor"]
    assert summary_cursor is not None
    assert _monthly(cursor=summary_cursor).status_code == 400
    # and a monthly cursor is not valid for the per-order summary either
    monthly_cursor = _monthly(limit=1).json()["next_cursor"]
    assert monthly_cursor is not None
    assert client.get("/stock-movements/summary", headers=H,
                      params={"cursor": monthly_cursor}).status_code == 400


def test_monthly_invalid_limit_is_rejected() -> None:
    assert _monthly(limit=0).status_code == 422
    assert _monthly(limit=-1).status_code == 422
    assert _monthly(limit=501).status_code == 422
    assert _monthly(limit="abc").status_code == 422


# ---------- consistency with movement operations ----------

def test_monthly_tracks_accept_reverse_and_replay_sequences() -> None:
    _make_order("smo10")
    _accept("sm10-in1", "smo10", "sm10-k1", direction="in", quantity=100)
    _accept("sm10-in2", "smo10", "sm10-k2", direction="in", quantity=50)
    _accept("sm10-out1", "smo10", "sm10-k3", direction="out", quantity=40)

    def net() -> int:
        return _monthly(order_id="smo10").json()["items"][0]["net_quantity"]

    assert net() == 110

    # replayed accepts (same fingerprint) do not count twice
    _accept("sm10-in1", "smo10", "sm10-k1", direction="in", quantity=100)
    assert net() == 110

    assert _reverse("sm10-out1", "sm10-rev1").status_code == 200
    assert net() == 150

    # replayed reverses do not flip or count twice
    assert _reverse("sm10-out1", "sm10-rev1").status_code == 200
    assert _reverse("sm10-out1", "sm10-rev2").status_code == 409
    assert net() == 150


def test_monthly_is_read_only_and_never_mutates_documents() -> None:
    _make_order("smo11")
    _accept("sm11-m1", "smo11", "sm11-k1", direction="in", quantity=60)

    before = {
        "order": client.get("/orders/smo11", headers=H).json(),
        "movement": client.get("/stock-movements/sm11-m1", headers=H).json(),
        "events": client.get("/stock-movements/sm11-m1/events", headers=H).json(),
        "summary": client.get("/stock-movements/summary", headers=H,
                              params={"order_id": "smo11"}).json(),
    }
    assert _monthly(order_id="smo11").status_code == 200
    assert _monthly(limit=500).status_code == 200
    after = {
        "order": client.get("/orders/smo11", headers=H).json(),
        "movement": client.get("/stock-movements/sm11-m1", headers=H).json(),
        "events": client.get("/stock-movements/sm11-m1/events", headers=H).json(),
        "summary": client.get("/stock-movements/summary", headers=H,
                              params={"order_id": "smo11"}).json(),
    }
    assert before == after
