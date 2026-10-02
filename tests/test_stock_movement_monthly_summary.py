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
TENANT = "smsm"
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


def _expected_monthly(order_id: str, tenant: str = TENANT) -> dict:
    """Independent per-(order, month) tally straight from the raw table."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT substr(created_at,1,7) AS month, "
            "COALESCE(SUM(CASE WHEN direction='in' AND status='accepted' THEN quantity ELSE 0 END),0) "
            "- COALESCE(SUM(CASE WHEN direction='out' AND status='accepted' THEN quantity ELSE 0 END),0) AS n "
            "FROM stock_movements WHERE tenant=? AND order_id=? GROUP BY month",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return {row["month"]: row["n"] for row in rows}


# ---------- aggregation ----------

def test_monthly_summary_nets_per_order_and_month() -> None:
    _make_order("mso1")
    _insert_direct("m1-a", "mso1", "in", 100, "2026-01-05T08:00:00+00:00")
    _insert_direct("m1-b", "mso1", "out", 40, "2026-01-20T08:00:00+00:00")
    _insert_direct("m1-c", "mso1", "in", 30, "2026-02-01T00:00:00+00:00")
    _insert_direct("m1-d", "mso1", "out", 5, "2025-12-31T23:59:59.999999+00:00")

    resp = _monthly(order_id="mso1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "items": [
            {"order_id": "mso1", "month": "2025-12", "net_quantity": -5},
            {"order_id": "mso1", "month": "2026-01", "net_quantity": 60},
            {"order_id": "mso1", "month": "2026-02", "net_quantity": 30},
        ],
        "next_cursor": None,
    }
    assert {item["month"]: item["net_quantity"] for item in resp.json()["items"]} == \
        _expected_monthly("mso1")


def test_monthly_summary_lists_one_row_per_order_month_sorted_stably() -> None:
    tenant = "smsm-sort"
    # insert deliberately out of both order and month order
    fixtures = [
        ("mso-z", "mz-2", "in", 1, "2026-03-01T00:00:00+00:00"),
        ("mso-a", "ma-2", "in", 1, "2026-02-01T00:00:00+00:00"),
        ("mso-a", "ma-1", "in", 1, "2026-01-01T00:00:00+00:00"),
        ("mso-m", "mm-1", "in", 1, "2026-01-01T00:00:00+00:00"),
        ("mso-z", "mz-1", "in", 1, "2026-01-01T00:00:00+00:00"),
        ("mso-a", "ma-3", "in", 1, "2026-03-01T00:00:00+00:00"),
    ]
    for oid in {oid for oid, *_ in fixtures}:
        _make_order(oid, tenant=tenant)
    for oid, mid, direction, qty, ts in fixtures:
        _insert_direct(mid, oid, direction, qty, ts, tenant=tenant)

    resp = _monthly(tenant, limit=500)
    assert resp.status_code == 200, resp.text
    keys = [(item["order_id"], item["month"]) for item in resp.json()["items"]]
    assert keys == sorted(keys)
    assert keys == [
        ("mso-a", "2026-01"), ("mso-a", "2026-02"), ("mso-a", "2026-03"),
        ("mso-m", "2026-01"),
        ("mso-z", "2026-01"), ("mso-z", "2026-03"),
    ]
    assert _monthly(tenant, limit=500).json() == resp.json()  # stable across repeated queries


def test_monthly_summary_reversed_movement_counts_zero_but_month_remains() -> None:
    _make_order("mso2")
    _insert_direct("m2-in", "mso2", "in", 30, "2026-01-10T00:00:00+00:00")
    _insert_direct("m2-out", "mso2", "out", 12, "2026-01-11T00:00:00+00:00")
    assert {i["month"]: i["net_quantity"] for i in
            _monthly(order_id="mso2").json()["items"]} == {"2026-01": 18}

    conn = connect()
    try:
        conn.execute("UPDATE stock_movements SET status='reversed' WHERE movement_id='m2-in'")
    finally:
        conn.close()
    items = _monthly(order_id="mso2").json()["items"]
    assert items == [{"order_id": "mso2", "month": "2026-01", "net_quantity": -12}]

    conn = connect()
    try:
        conn.execute("UPDATE stock_movements SET status='reversed' WHERE movement_id='m2-out'")
    finally:
        conn.close()
    # both reversed: the (order, month) entry stays with net 0
    assert _monthly(order_id="mso2").json()["items"] == [
        {"order_id": "mso2", "month": "2026-01", "net_quantity": 0}]


def test_monthly_nets_sum_to_the_per_order_summary() -> None:
    tenant = "smsm-sum"
    _make_order("mso3a", tenant=tenant)
    _make_order("mso3b", tenant=tenant)
    _insert_direct("m3-1", "mso3a", "in", 100, "2026-01-01T00:00:00+00:00", tenant=tenant)
    _insert_direct("m3-2", "mso3a", "out", 40, "2026-02-15T00:00:00+00:00", tenant=tenant)
    _insert_direct("m3-3", "mso3a", "in", 10, "2026-02-20T00:00:00+00:00", tenant=tenant)
    _insert_direct("m3-4", "mso3b", "out", 7, "2026-03-01T00:00:00+00:00", tenant=tenant)

    monthly = _monthly(tenant, limit=500).json()["items"]
    per_order = {i["order_id"]: i["net_quantity"]
                 for i in client.get("/stock-movements/summary",
                                     headers={"X-Tenant": tenant},
                                     params={"limit": 500}).json()["items"]}
    sums: dict[str, int] = {}
    for item in monthly:
        sums[item["order_id"]] = sums.get(item["order_id"], 0) + item["net_quantity"]
    assert sums == per_order == {"mso3a": 70, "mso3b": -7}


def test_monthly_summary_is_tenant_isolated() -> None:
    _make_order("mso4", tenant="t2m")
    _insert_direct("m4-a", "mso4", "in", 9, "2026-01-01T00:00:00+00:00", tenant="t2m")

    assert _monthly(order_id="mso4").status_code == 400  # no movements under this tenant
    assert _monthly(order_id="mso4", tenant="t2m").json()["items"] == [
        {"order_id": "mso4", "month": "2026-01", "net_quantity": 9}]
    assert all(item["order_id"] != "mso4" for item in _monthly(limit=500).json()["items"])


def test_monthly_summary_requires_tenant_header() -> None:
    assert client.get("/stock-movements/monthly-summary").status_code == 400


# ---------- single-order lookup ----------

def test_monthly_single_order_without_movements_is_400_without_leak() -> None:
    _make_order("mso5-exists-no-movements")
    existing = _monthly(order_id="mso5-exists-no-movements")
    ghost = _monthly(order_id="mso5-ghost")
    # identical response whether the order exists or not: no existence leak
    assert existing.status_code == ghost.status_code == 400
    assert existing.json() == ghost.json()


def test_monthly_single_order_paginates_its_months() -> None:
    _make_order("mso6")
    for month in ("2026-01", "2026-02", "2026-03"):
        _insert_direct(f"m6-{month}", "mso6", "in", 1, f"{month}-01T00:00:00+00:00")

    first = _monthly(order_id="mso6", limit=2)
    assert first.status_code == 200
    assert first.json()["items"] == [
        {"order_id": "mso6", "month": "2026-01", "net_quantity": 1},
        {"order_id": "mso6", "month": "2026-02", "net_quantity": 1},
    ]
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    tail = _monthly(order_id="mso6", limit=2, cursor=cursor)
    assert tail.json()["items"] == [
        {"order_id": "mso6", "month": "2026-03", "net_quantity": 1}]
    assert tail.json()["next_cursor"] is None


# ---------- listing ----------

def test_monthly_empty_result_is_empty_list_without_cursor() -> None:
    resp = _monthly(tenant="smsm-empty")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


# ---------- cursor pagination ----------

def _walk_pages(tenant: str, page_limit: int, **extra) -> list[dict]:
    items: list[dict] = []
    cursor = None
    seen_cursors = []
    while True:
        params = {"limit": page_limit, **extra}
        if cursor is not None:
            params["cursor"] = cursor
        resp = _monthly(tenant, **params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.append(cursor)
    return items


def test_monthly_pagination_covers_everything_once_in_stable_order() -> None:
    tenant = "smsm-page"
    expected = []
    for i in range(1, 5):
        oid = f"msp-{i:02d}"
        _make_order(oid, tenant=tenant)
        for j, month in enumerate(("2026-01", "2026-02", "2026-03")):
            _insert_direct(f"msp-{i}-{j}", oid, "in" if (i + j) % 2 else "out",
                           i * 10 + j, f"{month}-15T12:00:00+00:00", tenant=tenant)
            expected.append({
                "order_id": oid, "month": month,
                "net_quantity": (i * 10 + j) if (i + j) % 2 else -(i * 10 + j),
            })

    for page_limit in (1, 2, 3, 5, 12, 100):
        assert _walk_pages(tenant, page_limit) == expected

    # page size exactly equal to total -> no cursor; one under -> cursor
    assert _monthly(tenant, limit=12).json()["next_cursor"] is None
    assert _monthly(tenant, limit=11).json()["next_cursor"] is not None

    # resuming from a middle cursor yields exactly the matching tail
    first = _monthly(tenant, limit=4).json()
    assert first["items"] == expected[:4]
    rest = []
    cursor = first["next_cursor"]
    while cursor is not None:
        page = _monthly(tenant, limit=4, cursor=cursor).json()
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    assert rest == expected[4:]


def test_monthly_invalid_cursor_is_rejected() -> None:
    resp = _monthly(cursor="not-a-cursor")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor is invalid"

    # cursors issued by other stock-movement endpoints are not valid here
    _make_order("mso7")
    _accept("m7-a", "mso7", "m7-k1", direction="in", quantity=1)
    _accept("m7-b", "mso7", "m7-k2", direction="in", quantity=1)
    search_cursor = client.get("/stock-movements", headers=H,
                               params={"order_id": "mso7", "limit": 1}).json()["next_cursor"]
    assert _monthly(cursor=search_cursor).status_code == 400
    # need at least two orders for the per-order summary to issue a cursor
    _make_order("mso7b")
    _accept("m7-c", "mso7b", "m7-k3", direction="in", quantity=1)
    summary_cursor = client.get("/stock-movements/summary", headers=H,
                                params={"limit": 1}).json()["next_cursor"]
    assert _monthly(cursor=summary_cursor).status_code == 400

    # well-shaped base64/json but an illegal month is still invalid
    import base64
    import json
    bad_month = base64.urlsafe_b64encode(
        json.dumps({"order_id": "mso7", "month": "2026-13"}).encode()).decode()
    assert _monthly(cursor=bad_month).status_code == 400
    missing_field = base64.urlsafe_b64encode(
        json.dumps({"order_id": "mso7"}).encode()).decode()
    assert _monthly(cursor=missing_field).status_code == 400


def test_monthly_invalid_limit_is_rejected() -> None:
    assert _monthly(limit=0).status_code == 422
    assert _monthly(limit=-1).status_code == 422
    assert _monthly(limit=501).status_code == 422
    assert _monthly(limit="abc").status_code == 422


# ---------- consistency with movement operations ----------

def test_monthly_tracks_accept_reverse_and_replay_sequences() -> None:
    _make_order("mso8")
    # fix every movement into distinct months by direct insert, then drive
    # accept/reverse/replay through the API for one current-month movement too
    _insert_direct("m8-a", "mso8", "in", 100, "2026-01-01T00:00:00+00:00")
    _insert_direct("m8-b", "mso8", "out", 40, "2026-01-02T00:00:00+00:00")
    _insert_direct("m8-c", "mso8", "in", 50, "2026-02-01T00:00:00+00:00")

    def by_month() -> dict:
        return {i["month"]: i["net_quantity"]
                for i in _monthly(order_id="mso8").json()["items"]}

    assert by_month() == {"2026-01": 60, "2026-02": 50} == _expected_monthly("mso8")

    # reversing a movement zeroes it in its original month; month membership
    # (fixed at acceptance from created_at) never moves.
    conn = connect()
    try:
        conn.execute("UPDATE stock_movements SET status='reversed' WHERE movement_id='m8-b'")
    finally:
        conn.close()
    assert by_month() == {"2026-01": 100, "2026-02": 50} == _expected_monthly("mso8")

    # an accepted/replayed/reversed movement through the public API keeps the
    # monthly tally equal to the independent per-movement tally.
    live = _accept("m8-live", "mso8", "m8-k1", direction="in", quantity=25)
    live_month = live.json()["created_at"][:7]
    _accept("m8-live", "mso8", "m8-k1", direction="in", quantity=25)  # replay: no second count
    tally = by_month()
    assert tally == _expected_monthly("mso8") and tally[live_month] == 25
    assert _reverse("m8-live", "m8-rev1").status_code == 200
    assert _reverse("m8-live", "m8-rev1").status_code == 200  # replayed reverse
    assert _reverse("m8-live", "m8-rev2").status_code == 409
    # reversed: net 0 in its month, but the (order, month) entry survives
    tally = by_month()
    assert tally == _expected_monthly("mso8")
    assert tally[live_month] == 0
    assert tally["2026-01"] == 100 and tally["2026-02"] == 50


def test_monthly_summary_is_read_only_and_never_mutates_other_documents() -> None:
    _make_order("mso9", 1000)
    order_store.add_payment(TENANT, "mso9", 800)
    client.post("/refunds",
                json={"refund_id": "m9-r1", "order_id": "mso9",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "m9-r1"})
    client.post("/tickets",
                json={"ticket_id": "m9-w1", "order_id": "mso9", "issue": "异常"},
                headers={**H, "Idempotency-Key": "m9-tkt"})
    _accept("m9-m1", "mso9", "m9-m1", direction="in", quantity=60)

    before = {
        "order": client.get("/orders/mso9", headers=H).json(),
        "refund": client.get("/refunds/m9-r1", headers=H).json(),
        "ticket": client.get("/tickets/m9-w1", headers=H).json(),
        "movement": client.get("/stock-movements/m9-m1", headers=H).json(),
        "summary": client.get("/stock-movements/summary", headers=H).json(),
        "search": client.get("/stock-movements", headers=H).json(),
        "events": client.get("/stock-movements/m9-m1/events", headers=H).json(),
    }
    assert _monthly(order_id="mso9").status_code == 200
    assert _monthly().status_code == 200
    after = {
        "order": client.get("/orders/mso9", headers=H).json(),
        "refund": client.get("/refunds/m9-r1", headers=H).json(),
        "ticket": client.get("/tickets/m9-w1", headers=H).json(),
        "movement": client.get("/stock-movements/m9-m1", headers=H).json(),
        "summary": client.get("/stock-movements/summary", headers=H).json(),
        "search": client.get("/stock-movements", headers=H).json(),
        "events": client.get("/stock-movements/m9-m1/events", headers=H).json(),
    }
    assert before == after
