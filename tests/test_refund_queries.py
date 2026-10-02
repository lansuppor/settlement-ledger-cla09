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


def _make_order(order_id: str, amount: int = 1000, paid: int = 0,
                tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")
    if paid:
        order_store.add_payment(tenant, order_id, paid)


def _accept(refund_id: str, order_id: str, key: str, amount: int = 100,
            tenant: str = TENANT):
    return client.post(
        "/refunds",
        json={"refund_id": refund_id, "order_id": order_id,
              "amount_cents": amount, "reason": "t"},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _reverse(refund_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/refunds/{refund_id}/reverse",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _make_effective(refund_id: str, order_id: str, amount: int, tag: str,
                    tenant: str = TENANT) -> None:
    """Move a pending refund to effective via the settlement advance chain."""
    resp = client.post(
        "/settlements",
        json={"settlement_id": f"st-{tag}", "order_id": order_id,
              "amount_cents": amount, "refund_ids": [refund_id]},
        headers={"X-Tenant": tenant, "Idempotency-Key": f"stk-{tag}"},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/settlements/st-{tag}/advance",
                       headers={"X-Tenant": tenant, "Idempotency-Key": f"adv-{tag}"})
    assert resp.status_code == 200, resp.text


def _list(tenant: str = TENANT, **params):
    return client.get("/refunds", headers={"X-Tenant": tenant}, params=params)


def _summary(tenant: str = TENANT, **params):
    return client.get("/refunds/summary", headers={"X-Tenant": tenant}, params=params)


def _insert_direct(refund_id: str, order_id: str, amount: int, created_at: str,
                   tenant: str = TENANT, status: str = "pending") -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, reason, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, refund_id, order_id, amount, "", status, created_at),
        )
    finally:
        conn.close()


def _expected_refundable(order_id: str, tenant: str = TENANT) -> int:
    """Independent per-refund tally: paid minus pending/effective refund sum."""
    conn = connect()
    try:
        paid = conn.execute(
            "SELECT paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["paid_cents"]
        occupied = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM refunds "
            "WHERE tenant=? AND order_id=? AND status IN ('pending','effective')",
            (tenant, order_id),
        ).fetchone()["s"]
    finally:
        conn.close()
    return paid - occupied


def _paged(url_params: dict, tenant: str = TENANT):
    """Drain every page of GET /refunds, returning (items, page_cursors)."""
    items, cursors = [], []
    params = dict(url_params)
    while True:
        resp = _list(tenant, **params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items.extend(body["items"])
        if body["next_cursor"] is None:
            return items, cursors
        cursors.append(body["next_cursor"])
        params["cursor"] = body["next_cursor"]


# ---------- search: status visibility ----------

def test_default_visibility_returns_pending_and_effective_only() -> None:
    _make_order("rqo-vis", paid=900)
    _accept("rqv-1", "rqo-vis", "rqv-k1")
    _accept("rqv-2", "rqo-vis", "rqv-k2")
    _accept("rqv-3", "rqo-vis", "rqv-k3")
    _make_effective("rqv-2", "rqo-vis", 100, "vis")
    _reverse("rqv-3", "rqv-k3r")

    resp = _list(order_id="rqo-vis")
    assert resp.status_code == 200, resp.text
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqv-1", "rqv-2"]
    assert resp.json()["next_cursor"] is None


def test_include_reversed_true_returns_every_status() -> None:
    resp = _list(order_id="rqo-vis", include_reversed="true")
    assert resp.status_code == 200, resp.text
    assert {i["refund_id"] for i in resp.json()["items"]} == {"rqv-1", "rqv-2", "rqv-3"}


def test_status_filter_matches_exactly() -> None:
    resp = _list(order_id="rqo-vis", status="reversed")
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqv-3"]
    resp = _list(order_id="rqo-vis", status="effective")
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqv-2"]


def test_status_and_include_reversed_together_rejected() -> None:
    resp = _list(status="pending", include_reversed="true")
    assert resp.status_code == 400


def test_item_fields_match_point_read() -> None:
    one = client.get("/refunds/rqv-1", headers=H).json()
    listed = _list(order_id="rqo-vis", status="pending").json()["items"][0]
    assert listed == one


# ---------- search: filters ----------

def test_amount_range_is_inclusive_and_combinable() -> None:
    _make_order("rqo-amt", paid=900)
    _accept("rqa-1", "rqo-amt", "rqa-k1", amount=100)
    _accept("rqa-2", "rqo-amt", "rqa-k2", amount=200)
    _accept("rqa-3", "rqo-amt", "rqa-k3", amount=300)

    resp = _list(order_id="rqo-amt", amount_min=100, amount_max=200)
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqa-1", "rqa-2"]
    resp = _list(order_id="rqo-amt", amount_min=200)
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqa-2", "rqa-3"]
    resp = _list(order_id="rqo-amt", amount_max=100)
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqa-1"]


def test_reversed_amount_range_rejected() -> None:
    resp = _list(amount_min=300, amount_max=100)
    assert resp.status_code == 400


def test_created_range_is_inclusive() -> None:
    _make_order("rqo-time", paid=900)
    _insert_direct("rqt-1", "rqo-time", 10, "2026-01-01T00:00:00+00:00")
    _insert_direct("rqt-2", "rqo-time", 10, "2026-02-01T00:00:00+00:00")
    _insert_direct("rqt-3", "rqo-time", 10, "2026-03-01T00:00:00+00:00")

    resp = _list(order_id="rqo-time", created_from="2026-02-01T00:00:00+00:00",
                 created_to="2026-03-01T00:00:00+00:00")
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqt-2", "rqt-3"]


def test_reversed_created_range_rejected() -> None:
    resp = _list(created_from="2026-03-01T00:00:00+00:00",
                 created_to="2026-01-01T00:00:00+00:00")
    assert resp.status_code == 400


# ---------- search: ordering and pagination ----------

def test_ordering_is_created_then_id_regardless_of_insertion_order() -> None:
    _make_order("rqo-ord", paid=900)
    _insert_direct("rqo-b", "rqo-ord", 10, "2026-01-02T00:00:00+00:00")
    _insert_direct("rqo-d", "rqo-ord", 10, "2026-01-01T00:00:00+00:00")
    _insert_direct("rqo-a", "rqo-ord", 10, "2026-01-02T00:00:00+00:00")
    _insert_direct("rqo-c", "rqo-ord", 10, "2026-01-01T00:00:00+00:00")

    resp = _list(order_id="rqo-ord", include_reversed="true")
    assert [i["refund_id"] for i in resp.json()["items"]] == [
        "rqo-c", "rqo-d", "rqo-a", "rqo-b"]


def test_pagination_unions_to_full_set_without_gaps_or_duplicates() -> None:
    _make_order("rqo-page", amount=9000, paid=9000)
    for i in range(7):
        _insert_direct(f"rqp-{i}", "rqo-page", 10, "2026-01-01T00:00:00+00:00",
                       status="pending" if i % 2 == 0 else "reversed")

    full = _list(order_id="rqo-page", include_reversed="true", limit=500).json()["items"]
    for size in (1, 2, 3, 6, 7, 8):
        items, _ = _paged({"order_id": "rqo-page", "include_reversed": "true",
                           "limit": size})
        assert items == full
        assert len({i["refund_id"] for i in items}) == len(items)


def test_cursor_points_at_last_row_of_previous_page() -> None:
    _make_order("rqo-cur", amount=9000, paid=9000)
    _insert_direct("rqc-1", "rqo-cur", 10, "2026-01-01T00:00:00+00:00")
    _insert_direct("rqc-2", "rqo-cur", 10, "2026-01-02T00:00:00+00:00")
    _insert_direct("rqc-3", "rqo-cur", 10, "2026-01-03T00:00:00+00:00")

    first = _list(order_id="rqo-cur", limit=2).json()
    assert [i["refund_id"] for i in first["items"]] == ["rqc-1", "rqc-2"]
    assert first["next_cursor"] is not None
    second = _list(order_id="rqo-cur", limit=2, cursor=first["next_cursor"]).json()
    assert [i["refund_id"] for i in second["items"]] == ["rqc-3"]
    assert second["next_cursor"] is None


def test_empty_result_returns_empty_items_and_null_cursor() -> None:
    resp = _list(order_id="rqo-no-such-order")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": [], "next_cursor": None}


# ---------- search: validation and isolation ----------

def test_invalid_params_rejected() -> None:
    assert _list().status_code == 200  # baseline with tenant header
    assert client.get("/refunds").status_code == 400  # missing tenant header
    assert _list(status="bogus").status_code == 422
    assert _list(include_reversed="notabool").status_code == 422
    assert _list(limit=0).status_code == 422
    assert _list(limit=501).status_code == 422
    assert _list(limit="abc").status_code == 422
    assert _list(amount_min=0).status_code == 422
    assert _list(cursor="!!!not-a-cursor!!!").status_code == 400


def test_search_is_tenant_isolated() -> None:
    _make_order("rqo-iso", paid=900, tenant="rq-other")
    _accept("rqi-1", "rqo-iso", "rqi-k1", tenant="rq-other")
    resp = _list(order_id="rqo-iso", include_reversed="true")
    assert resp.json() == {"items": [], "next_cursor": None}
    resp = client.get("/refunds", headers={"X-Tenant": "rq-other"},
                      params={"order_id": "rqo-iso"})
    assert [i["refund_id"] for i in resp.json()["items"]] == ["rqi-1"]


# ---------- summary ----------

def test_summary_single_order_matches_per_refund_tally() -> None:
    _make_order("rqo-sum", amount=1000, paid=800)
    _accept("rqs-1", "rqo-sum", "rqs-k1", amount=100)   # pending
    _accept("rqs-2", "rqo-sum", "rqs-k2", amount=200)   # effective
    _accept("rqs-3", "rqo-sum", "rqs-k3", amount=50)    # reversed
    _make_effective("rqs-2", "rqo-sum", 200, "sum")
    _reverse("rqs-3", "rqs-k3r")

    resp = _summary(order_id="rqo-sum")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["next_cursor"] is None
    assert body["items"] == [{"order_id": "rqo-sum",
                              "refundable_cents": _expected_refundable("rqo-sum")}]
    assert body["items"][0]["refundable_cents"] == 800 - 100 - 200


def test_summary_tracks_accept_reverse_advance_and_revoke() -> None:
    _make_order("rqo-lc", amount=1000, paid=600)
    _accept("rql-1", "rqo-lc", "rql-k1", amount=120)
    assert _summary(order_id="rqo-lc").json()["items"][0]["refundable_cents"] == 480

    _make_effective("rql-1", "rqo-lc", 120, "lc")
    assert _summary(order_id="rqo-lc").json()["items"][0]["refundable_cents"] == 480

    # Revoking the settlement returns the refund to pending: quota unchanged.
    client.post("/settlements/st-lc/revoke",
                headers={"X-Tenant": TENANT, "Idempotency-Key": "rvk-lc"})
    assert _summary(order_id="rqo-lc").json()["items"][0]["refundable_cents"] == 480

    # Reversing the refund releases the quota.
    _reverse("rql-1", "rql-k1r")
    after = _summary(order_id="rqo-lc").json()["items"][0]["refundable_cents"]
    assert after == 600 == _expected_refundable("rqo-lc")


def test_summary_single_order_without_refunds_is_indistinguishable_from_missing() -> None:
    _make_order("rqo-norf", paid=100)
    resp = _summary(order_id="rqo-norf")
    assert resp.status_code == 400
    assert _summary(order_id="rqo-never-existed").status_code == 400
    assert resp.json() == _summary(order_id="rqo-never-existed").json()


def test_summary_all_orders_paginates_by_order_id() -> None:
    _make_order("rqo-pa", paid=100)
    _make_order("rqo-pb", paid=100)
    _make_order("rqo-pc", paid=100)
    _accept("rqpa-1", "rqo-pa", "rqpa-k1", amount=10)
    _accept("rqpb-1", "rqo-pb", "rqpb-k1", amount=20)
    _accept("rqpc-1", "rqo-pc", "rqpc-k1", amount=30)

    full = _summary(limit=500).json()["items"]
    mine = [i for i in full if i["order_id"] in {"rqo-pa", "rqo-pb", "rqo-pc"}]
    assert [i["order_id"] for i in mine] == ["rqo-pa", "rqo-pb", "rqo-pc"]
    assert mine[0]["refundable_cents"] == 90

    # Drain page by page; union equals the full listing, strictly increasing.
    seen, cursor = [], None
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = _summary(**params).json()
        seen.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == full
    ids = [i["order_id"] for i in seen]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_summary_empty_result_and_validation() -> None:
    assert _summary(limit=0).status_code == 422
    assert _summary(limit=501).status_code == 422
    assert _summary(cursor="!!!bad!!!").status_code == 400
    assert client.get("/refunds/summary").status_code == 400  # missing tenant
    body = _summary(tenant="rq-empty").json()
    assert body == {"items": [], "next_cursor": None}


def test_summary_is_tenant_isolated() -> None:
    resp = _summary(order_id="rqo-iso")  # only exists under tenant rq-other
    assert resp.status_code == 400
    resp = client.get("/refunds/summary", headers={"X-Tenant": "rq-other"},
                      params={"order_id": "rqo-iso"})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["order_id"] == "rqo-iso"


def test_queries_are_read_only() -> None:
    _make_order("rqo-ro", amount=500, paid=400)
    _accept("rqro-1", "rqo-ro", "rqro-k1", amount=100)
    before_order = client.get("/orders/rqo-ro", headers=H).json()
    before_refund = client.get("/refunds/rqro-1", headers=H).json()

    _list(order_id="rqo-ro", include_reversed="true")
    _summary(order_id="rqo-ro")

    assert client.get("/orders/rqo-ro", headers=H).json() == before_order
    assert client.get("/refunds/rqro-1", headers=H).json() == before_refund
