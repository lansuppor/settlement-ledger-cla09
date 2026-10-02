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
TENANT = "rq"
H = {"X-Tenant": TENANT}


def _make_paid_order(order_id: str, amount: int = 1000, paid: int | None = None,
                     tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")
    order_store.add_payment(tenant, order_id, paid if paid is not None else amount)


def _refund(refund_id: str, order_id: str, amount: int, key: str,
            reason: str = "customer request", tenant: str = TENANT):
    return client.post(
        "/refunds",
        json={"refund_id": refund_id, "order_id": order_id,
              "amount_cents": amount, "reason": reason},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _reverse(refund_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/refunds/{refund_id}/reverse",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _settle_and_advance(settlement_id: str, order_id: str, refund_ids: list[str],
                        amount: int, key: str, tenant: str = TENANT):
    created = client.post(
        "/settlements",
        json={"settlement_id": settlement_id, "order_id": order_id,
              "amount_cents": amount, "refund_ids": refund_ids, "reason": "周期结算"},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    return client.post(f"/settlements/{settlement_id}/advance",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key + "-adv"})


def _revoke(settlement_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/settlements/{settlement_id}/revoke",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _search(tenant: str = TENANT, **params):
    return client.get("/refunds", headers={"X-Tenant": tenant}, params=params)


def _summary(tenant: str = TENANT, **params):
    return client.get("/refunds/summary", headers={"X-Tenant": tenant}, params=params)


def _insert_direct(refund_id: str, order_id: str, amount: int, created_at: str,
                   tenant: str = TENANT, status: str = "pending",
                   reason: str = "customer request") -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, reason, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, refund_id, order_id, amount, reason, status, created_at),
        )
    finally:
        conn.close()


def _expected_refundable(order_id: str, tenant: str = TENANT) -> int:
    """Independent per-refund tally: paid minus pending/effective refund sum."""
    conn = connect()
    try:
        return conn.execute(
            "SELECT o.paid_cents - COALESCE(SUM(CASE WHEN r.status IN ('pending','effective') "
            "THEN r.amount_cents ELSE 0 END),0) AS v "
            "FROM orders o LEFT JOIN refunds r ON r.tenant=o.tenant AND r.order_id=o.order_id "
            "WHERE o.tenant=? AND o.order_id=? GROUP BY o.order_id",
            (tenant, order_id),
        ).fetchone()["v"]
    finally:
        conn.close()


# ---------- search: status visibility ----------

def test_search_default_returns_pending_and_effective_only() -> None:
    _make_paid_order("rqo1", 1000)
    _refund("rq1-p", "rqo1", 100, "rq1-k1")
    _refund("rq1-e", "rqo1", 100, "rq1-k2")
    _refund("rq1-r", "rqo1", 100, "rq1-k3")
    _settle_and_advance("rq1-s1", "rqo1", ["rq1-e"], 100, "rq1-sk")
    _reverse("rq1-r", "rq1-rev")

    resp = _search(order_id="rqo1")
    assert resp.status_code == 200, resp.text
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rq1-p", "rq1-e"]
    assert resp.json()["next_cursor"] is None

    everything = _search(order_id="rqo1", include_reversed="true").json()["items"]
    assert [i["refund_id"] for i in everything] == ["rq1-p", "rq1-e", "rq1-r"]
    assert {i["refund_id"]: i["status"] for i in everything} == {
        "rq1-p": "pending", "rq1-e": "effective", "rq1-r": "reversed"}


def test_search_status_filter_is_exact_and_combines_with_other_filters() -> None:
    _make_paid_order("rqo2", 1000)
    _refund("rq2-a", "rqo2", 100, "rq2-k1")
    _refund("rq2-b", "rqo2", 200, "rq2-k2")
    _settle_and_advance("rq2-s1", "rqo2", ["rq2-b"], 200, "rq2-sk")

    assert [i["refund_id"] for i in _search(order_id="rqo2", status="pending").json()["items"]] == ["rq2-a"]
    assert [i["refund_id"] for i in _search(order_id="rqo2", status="effective").json()["items"]] == ["rq2-b"]
    assert _search(order_id="rqo2", status="reversed").json()["items"] == []
    # status is ANDed with the remaining filters
    assert _search(order_id="rqo2", status="pending", amount_min=150).json()["items"] == []
    assert [i["refund_id"] for i in
            _search(status="effective", amount_min=150, amount_max=200).json()["items"]] == ["rq2-b"]


def test_search_status_and_include_reversed_together_is_400() -> None:
    assert _search(status="pending", include_reversed="true").status_code == 400
    assert _search(status="reversed", include_reversed="false").status_code == 400


def test_search_item_fields_match_single_read() -> None:
    _make_paid_order("rqo3", 500)
    _refund("rq3-a", "rqo3", 120, "rq3-k1", reason="破损")
    listed = _search(order_id="rqo3").json()["items"][0]
    assert listed == client.get("/refunds/rq3-a", headers=H).json()
    assert set(listed) == {"refund_id", "order_id", "amount_cents", "reason",
                           "status", "created_at"}


# ---------- search: filters ----------

def test_search_amount_range_is_inclusive_and_must_not_reverse() -> None:
    _make_paid_order("rqo4", 1000)
    for rid, amount in (("rq4-a", 100), ("rq4-b", 200), ("rq4-c", 300)):
        _refund(rid, "rqo4", amount, f"{rid}-k")

    assert [i["refund_id"] for i in
            _search(order_id="rqo4", amount_min=200).json()["items"]] == ["rq4-b", "rq4-c"]
    assert [i["refund_id"] for i in
            _search(order_id="rqo4", amount_max=200).json()["items"]] == ["rq4-a", "rq4-b"]
    # endpoints are inclusive
    assert [i["refund_id"] for i in
            _search(order_id="rqo4", amount_min=200, amount_max=200).json()["items"]] == ["rq4-b"]
    assert _search(order_id="rqo4", amount_min=300, amount_max=100).status_code == 400
    assert _search(order_id="rqo4", amount_min=0).status_code == 422
    assert _search(order_id="rqo4", amount_max=-5).status_code == 422
    assert _search(order_id="rqo4", amount_min="abc").status_code == 422


def test_search_created_range_is_inclusive_and_must_not_reverse() -> None:
    _make_paid_order("rqo5", 1000)
    _insert_direct("rq5-a", "rqo5", 100, "2026-01-01T00:00:00+00:00")
    _insert_direct("rq5-b", "rqo5", 100, "2026-01-02T00:00:00+00:00")
    _insert_direct("rq5-c", "rqo5", 100, "2026-01-03T00:00:00+00:00")

    assert [i["refund_id"] for i in _search(
        order_id="rqo5", created_from="2026-01-02T00:00:00+00:00").json()["items"]] == ["rq5-b", "rq5-c"]
    assert [i["refund_id"] for i in _search(
        order_id="rqo5", created_to="2026-01-02T00:00:00+00:00").json()["items"]] == ["rq5-a", "rq5-b"]
    # endpoints are inclusive
    assert [i["refund_id"] for i in _search(
        order_id="rqo5", created_from="2026-01-02T00:00:00+00:00",
        created_to="2026-01-02T00:00:00+00:00").json()["items"]] == ["rq5-b"]
    assert _search(order_id="rqo5", created_from="2026-01-03T00:00:00+00:00",
                   created_to="2026-01-01T00:00:00+00:00").status_code == 400
    assert _search(order_id="rqo5", created_from="not-a-time").status_code == 400
    assert _search(order_id="rqo5", created_to="2026-13-99").status_code == 400


def test_search_rejects_invalid_params() -> None:
    assert _search(status="settled").status_code == 422
    assert _search(include_reversed="maybe").status_code == 422
    assert _search(limit=0).status_code == 422
    assert _search(limit=-1).status_code == 422
    assert _search(limit=501).status_code == 422
    assert _search(limit="abc").status_code == 422
    assert client.get("/refunds").status_code == 400  # tenant header missing


def test_search_is_tenant_isolated() -> None:
    _make_paid_order("rqo6", 1000, tenant="t2")
    _refund("rq6-a", "rqo6", 100, "rq6-k1", tenant="t2")

    assert all(i["order_id"] != "rqo6" for i in _search(limit=500).json()["items"])
    assert [i["refund_id"] for i in _search(tenant="t2", order_id="rqo6").json()["items"]] == ["rq6-a"]


def test_search_empty_result_is_empty_list_without_cursor() -> None:
    resp = _search(tenant="rq-empty")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


# ---------- search: ordering & pagination ----------

def test_search_orders_by_created_at_then_refund_id_regardless_of_insertion() -> None:
    _make_paid_order("rqo7", 1000)
    ts = "2026-03-01T00:00:00+00:00"
    # insert deliberately out of order, including identical timestamps
    _insert_direct("rq7-z", "rqo7", 10, "2026-03-02T00:00:00+00:00")
    _insert_direct("rq7-b", "rqo7", 10, ts)
    _insert_direct("rq7-a", "rqo7", 10, ts)
    _insert_direct("rq7-m", "rqo7", 10, "2026-03-01T00:00:01+00:00")

    resp = _search(order_id="rqo7")
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rq7-a", "rq7-b", "rq7-m", "rq7-z"]
    assert _search(order_id="rqo7").json() == resp.json()  # stable across repeated queries


def _walk_search_pages(tenant: str, page_limit: int, **params) -> list[dict]:
    items: list[dict] = []
    cursor = None
    seen_cursors = []
    while True:
        query = {"limit": page_limit, **params}
        if cursor is not None:
            query["cursor"] = cursor
        resp = _search(tenant, **query)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert cursor not in seen_cursors
        seen_cursors.append(cursor)
    return items


def test_search_pagination_covers_everything_once_in_stable_order() -> None:
    tenant = "rq-page"
    _make_paid_order("rqp-o", 10_000, tenant=tenant)
    ts = "2026-04-01T00:00:00+00:00"
    expected_ids = []
    for i in range(1, 8):
        rid = f"rqp-r{i}"
        # identical timestamps force the refund_id tie-breaker
        _insert_direct(rid, "rqp-o", i * 10, ts, tenant=tenant,
                       status="reversed" if i % 3 == 0 else "pending")
        expected_ids.append(rid)

    expected_all = _search(tenant, order_id="rqp-o", include_reversed="true",
                           limit=500).json()["items"]
    assert [i["refund_id"] for i in expected_all] == sorted(expected_ids)

    for page_limit in (1, 2, 3, 5, 100):
        assert _walk_search_pages(tenant, page_limit, order_id="rqp-o",
                                  include_reversed="true") == expected_all

    # default visibility paginates over pending/effective only
    visible = [i for i in expected_all if i["status"] != "reversed"]
    for page_limit in (1, 2, 3, 100):
        assert _walk_search_pages(tenant, page_limit, order_id="rqp-o") == visible

    # page size exactly equal to total -> no cursor; one under -> cursor
    assert _search(tenant, order_id="rqp-o", include_reversed="true",
                   limit=7).json()["next_cursor"] is None
    assert _search(tenant, order_id="rqp-o", include_reversed="true",
                   limit=6).json()["next_cursor"] is not None

    # resuming from a middle cursor yields exactly the matching tail
    first = _search(tenant, order_id="rqp-o", include_reversed="true", limit=3).json()
    assert first["items"] == expected_all[:3]
    rest = []
    cursor = first["next_cursor"]
    while cursor is not None:
        page = _search(tenant, order_id="rqp-o", include_reversed="true",
                       limit=3, cursor=cursor).json()
        rest.extend(page["items"])
        cursor = page["next_cursor"]
    assert rest == expected_all[3:]


def test_search_invalid_cursor_is_rejected() -> None:
    resp = _search(cursor="not-a-cursor")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor is invalid"
    # a cursor issued by the summary endpoint is not valid here either
    _make_paid_order("rqo8", 1000)
    _refund("rq8-a", "rqo8", 100, "rq8-k1")
    _refund("rq8-b", "rqo8", 100, "rq8-k2")
    summary_cursor = _summary(limit=1).json()["next_cursor"]
    assert summary_cursor is not None
    assert _search(cursor=summary_cursor).status_code == 400


# ---------- summary: aggregation ----------

def test_summary_refundable_is_paid_minus_pending_and_effective() -> None:
    _make_paid_order("rqso1", 1000, paid=800)
    _refund("rqs1-a", "rqso1", 100, "rqs1-k1")            # pending
    _refund("rqs1-b", "rqso1", 200, "rqs1-k2")            # effective
    _refund("rqs1-c", "rqso1", 50, "rqs1-k3")             # reversed
    _settle_and_advance("rqs1-s1", "rqso1", ["rqs1-b"], 200, "rqs1-sk")
    _reverse("rqs1-c", "rqs1-rev")

    resp = _summary(order_id="rqso1")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [{"order_id": "rqso1", "refundable_cents": 500}],
                           "next_cursor": None}
    assert resp.json()["items"][0]["refundable_cents"] == _expected_refundable("rqso1")


def test_summary_tracks_accept_reverse_advance_and_revoke_sequences() -> None:
    _make_paid_order("rqso2", 1000)
    _refund("rqs2-a", "rqso2", 100, "rqs2-k1")
    _refund("rqs2-b", "rqso2", 200, "rqs2-k2")

    def refundable() -> int:
        return _summary(order_id="rqso2").json()["items"][0]["refundable_cents"]

    assert refundable() == 700 == _expected_refundable("rqso2")

    # replayed accepts (same fingerprint) do not count twice
    _refund("rqs2-a", "rqso2", 100, "rqs2-k1")
    assert refundable() == 700

    # advancing pending -> effective keeps the quota occupied
    _settle_and_advance("rqs2-s1", "rqso2", ["rqs2-a", "rqs2-b"], 300, "rqs2-sk")
    assert refundable() == 700 == _expected_refundable("rqso2")

    # revoking the settlement flips effective refunds back to pending
    assert _revoke("rqs2-s1", "rqs2-rev-s").status_code == 200
    assert refundable() == 700 == _expected_refundable("rqso2")

    # reversing releases the quota; replayed reverses do not release twice
    assert _reverse("rqs2-a", "rqs2-rev1").status_code == 200
    assert refundable() == 800 == _expected_refundable("rqso2")
    assert _reverse("rqs2-a", "rqs2-rev1").status_code == 200
    assert refundable() == 800


def test_summary_is_tenant_isolated() -> None:
    _make_paid_order("rqso3", 1000, tenant="t2")
    _refund("rqs3-a", "rqso3", 100, "rqs3-k1", tenant="t2")

    assert _summary(order_id="rqso3").status_code == 400  # no refunds under this tenant
    assert _summary(order_id="rqso3", tenant="t2").json()["items"] == [
        {"order_id": "rqso3", "refundable_cents": 900}]
    assert all(item["order_id"] != "rqso3" for item in _summary().json()["items"])


def test_summary_requires_tenant_header() -> None:
    assert client.get("/refunds/summary").status_code == 400


# ---------- summary: single-order lookup ----------

def test_summary_single_order_without_refunds_is_400_without_leak() -> None:
    _make_paid_order("rqso4-exists-no-refunds")
    existing = _summary(order_id="rqso4-exists-no-refunds")
    ghost = _summary(order_id="rqso4-ghost")
    # identical response whether the order exists or not: no existence leak
    assert existing.status_code == ghost.status_code == 400
    assert existing.json() == ghost.json()


def test_summary_single_order_ignores_pagination_params() -> None:
    _make_paid_order("rqso5", 1000)
    _refund("rqs5-a", "rqso5", 100, "rqs5-k1")
    resp = _summary(order_id="rqso5", limit=1)
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"order_id": "rqso5", "refundable_cents": 900}],
                           "next_cursor": None}


# ---------- summary: all-orders listing & pagination ----------

def test_summary_orders_sort_by_order_id_regardless_of_insertion() -> None:
    ts = "2026-05-01T00:00:00+00:00"
    # insert deliberately out of order
    for oid, rid in (("rqso6-z", "rqs6-1"), ("rqso6-a", "rqs6-2"), ("rqso6-m", "rqs6-3")):
        _make_paid_order(oid, 1000)
        _insert_direct(rid, oid, 100, ts)

    resp = _summary(limit=500)
    assert resp.status_code == 200, resp.text
    order_ids = [item["order_id"] for item in resp.json()["items"]]
    assert order_ids == sorted(order_ids)
    assert order_ids.index("rqso6-a") < order_ids.index("rqso6-m") < order_ids.index("rqso6-z")
    assert _summary(limit=500).json() == resp.json()  # stable across repeated queries


def test_summary_empty_result_is_empty_list_without_cursor() -> None:
    resp = _summary(tenant="rq-sum-empty")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


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
    tenant = "rq-sum-page"
    ts = "2026-06-01T00:00:00+00:00"
    expected = []
    for i in range(1, 8):
        oid = f"rqsp-{i:02d}"
        _make_paid_order(oid, 1000, tenant=tenant)
        _insert_direct(f"rqsp-r{i}", oid, i * 10, ts, tenant=tenant,
                       status="reversed" if i % 3 == 0 else "pending")
        occupied = 0 if i % 3 == 0 else i * 10
        expected.append({"order_id": oid, "refundable_cents": 1000 - occupied})

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


def test_summary_invalid_cursor_and_limit_are_rejected() -> None:
    resp = _summary(cursor="not-a-cursor")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "cursor is invalid"
    # a cursor issued by the refund search endpoint is not valid here either
    _make_paid_order("rqso7", 1000)
    _refund("rqs7-a", "rqso7", 100, "rqs7-k1")
    _refund("rqs7-b", "rqso7", 100, "rqs7-k2")
    search_cursor = _search(order_id="rqso7", limit=1).json()["next_cursor"]
    assert search_cursor is not None
    assert _summary(cursor=search_cursor).status_code == 400

    assert _summary(limit=0).status_code == 422
    assert _summary(limit=-1).status_code == 422
    assert _summary(limit=501).status_code == 422
    assert _summary(limit="abc").status_code == 422


# ---------- read-only guarantees ----------

def test_queries_are_read_only_and_never_mutate_other_documents() -> None:
    _make_paid_order("rqro1", 1000, paid=800)
    _refund("rqro-r1", "rqro1", 200, "rqro-k1")
    _settle_and_advance("rqro-s1", "rqro1", ["rqro-r1"], 200, "rqro-sk")
    client.post("/tickets",
                json={"ticket_id": "rqro-w1", "order_id": "rqro1", "issue": "异常"},
                headers={**H, "Idempotency-Key": "rqro-tkt"})

    before = {
        "order": client.get("/orders/rqro1", headers=H).json(),
        "refund": client.get("/refunds/rqro-r1", headers=H).json(),
        "settlement": client.get("/settlements/rqro-s1", headers=H).json(),
        "ticket": client.get("/tickets/rqro-w1", headers=H).json(),
    }
    assert _search(order_id="rqro1").status_code == 200
    assert _search(order_id="rqro1", include_reversed="true").status_code == 200
    assert _summary(order_id="rqro1").status_code == 200
    assert _summary().status_code == 200
    after = {
        "order": client.get("/orders/rqro1", headers=H).json(),
        "refund": client.get("/refunds/rqro-r1", headers=H).json(),
        "settlement": client.get("/settlements/rqro-s1", headers=H).json(),
        "ticket": client.get("/tickets/rqro-w1", headers=H).json(),
    }
    assert before == after
