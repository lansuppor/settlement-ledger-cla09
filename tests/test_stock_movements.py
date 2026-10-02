import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders as order_store
from app.store.db import migrate

migrate()
client = TestClient(app)

# Dedicated tenant so this module's fixtures never collide with the other suites.
TENANT = "sm"
OTHER = "sm-other"
H = {"X-Tenant": TENANT}

MOVEMENT_FIELDS = {"movement_id", "order_id", "direction", "quantity", "created_at"}


def _make_order(order_id: str, amount: int = 1000, tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")


def _accept(movement_id: str, order_id: str, key: str, direction: str = "in",
            quantity: int = 5, tenant: str = TENANT):
    return client.post(
        "/stock-movements",
        json={"movement_id": movement_id, "order_id": order_id,
              "direction": direction, "quantity": quantity},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _get(movement_id: str, tenant: str = TENANT):
    return client.get(f"/stock-movements/{movement_id}", headers={"X-Tenant": tenant})


def _search(tenant: str = TENANT, **params):
    return client.get("/stock-movements",
                      params={k: v for k, v in params.items() if v is not None},
                      headers={"X-Tenant": tenant})


def _ids(body: dict) -> list[str]:
    return [item["movement_id"] for item in body["items"]]


def test_accept_success_returns_201_and_preserves_values():
    _make_order("sm-o1")
    resp = _accept("sm-m1", "sm-o1", "k-m1", direction="out", quantity=7)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == MOVEMENT_FIELDS
    assert body["movement_id"] == "sm-m1"
    assert body["order_id"] == "sm-o1"
    assert body["direction"] == "out"
    assert body["quantity"] == 7
    assert body["created_at"]


def test_accept_missing_order_returns_404():
    resp = _accept("sm-m-missing", "sm-no-such-order", "k-missing")
    assert resp.status_code == 404


def test_accept_cross_tenant_order_returns_404():
    _make_order("sm-o-other", tenant=OTHER)
    resp = _accept("sm-m-xtenant", "sm-o-other", "k-xtenant")
    assert resp.status_code == 404


def test_accept_duplicate_id_returns_409_and_keeps_existing():
    _make_order("sm-o2")
    first = _accept("sm-m2", "sm-o2", "k-m2", direction="in", quantity=3)
    assert first.status_code == 201
    dup = _accept("sm-m2", "sm-o2", "k-m2-other", direction="out", quantity=99)
    assert dup.status_code == 409
    kept = _get("sm-m2").json()
    assert kept["direction"] == "in"
    assert kept["quantity"] == 3


def test_accept_replay_returns_identical_result():
    _make_order("sm-o3")
    first = _accept("sm-m3", "sm-o3", "k-m3", quantity=4)
    replay = _accept("sm-m3", "sm-o3", "k-m3", quantity=4)
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()


def test_accept_replay_returns_identical_first_error():
    first = _accept("sm-m-err", "sm-no-order", "k-err")
    assert first.status_code == 404
    # The order now exists, but the replayed request still returns the first error.
    _make_order("sm-no-order")
    replay = _accept("sm-m-err", "sm-no-order", "k-err")
    assert replay.status_code == 404
    assert replay.json() == first.json()
    assert _get("sm-m-err").status_code == 404


def test_accept_concurrent_different_keys_only_one_wins():
    _make_order("sm-o4")
    results = []

    def submit(key: str) -> None:
        results.append(_accept("sm-m4", "sm-o4", key, quantity=2).status_code)

    threads = [threading.Thread(target=submit, args=(f"k-conc-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [201, 409, 409, 409]


def test_read_by_id_and_cross_tenant_isolation():
    _make_order("sm-o5")
    _accept("sm-m5", "sm-o5", "k-m5", direction="out", quantity=9)
    body = _get("sm-m5").json()
    assert set(body) == MOVEMENT_FIELDS
    assert body["direction"] == "out" and body["quantity"] == 9
    assert _get("sm-m5", tenant=OTHER).status_code == 404
    assert _get("sm-m-absent").status_code == 404


def test_accept_invalid_params():
    _make_order("sm-o6")
    bad_direction = _accept("sm-m-bad-dir", "sm-o6", "k-bad-dir", direction="sideways")
    assert bad_direction.status_code == 422
    zero = _accept("sm-m-zero", "sm-o6", "k-zero", quantity=0)
    assert zero.status_code == 422
    negative = _accept("sm-m-neg", "sm-o6", "k-neg", quantity=-3)
    assert negative.status_code == 422
    missing_key = client.post(
        "/stock-movements",
        json={"movement_id": "sm-m-nokey", "order_id": "sm-o6",
              "direction": "in", "quantity": 1},
        headers=H,
    )
    assert missing_key.status_code == 400


def _seed_search_fixtures() -> None:
    _make_order("sm-oa")
    _make_order("sm-ob")
    # Distinct quantities and directions across two orders.
    _accept("sm-s1", "sm-oa", "k-s1", direction="in", quantity=10)
    _accept("sm-s2", "sm-oa", "k-s2", direction="out", quantity=20)
    _accept("sm-s3", "sm-ob", "k-s3", direction="in", quantity=30)
    _accept("sm-s4", "sm-ob", "k-s4", direction="out", quantity=40)


def test_search_filters_combine_with_and():
    _seed_search_fixtures()
    by_order = _search(order_id="sm-oa").json()
    assert set(_ids(by_order)) == {"sm-s1", "sm-s2"}
    by_direction = _search(direction="out").json()
    assert {"sm-s2", "sm-s4"} <= set(_ids(by_direction))
    by_quantity = _search(quantity_min=15, quantity_max=35).json()
    assert {"sm-s2", "sm-s3"} <= set(_ids(by_quantity))
    assert "sm-s1" not in _ids(by_quantity)
    assert "sm-s4" not in _ids(by_quantity)
    combined = _search(order_id="sm-ob", direction="in",
                       quantity_min=30, quantity_max=30).json()
    assert _ids(combined) == ["sm-s3"]
    # Range endpoints are inclusive.
    inclusive = _search(quantity_min=10, quantity_max=40, order_id="sm-oa").json()
    assert set(_ids(inclusive)) == {"sm-s1", "sm-s2"}


def test_search_created_at_range_inclusive():
    _make_order("sm-oc")
    first = _accept("sm-t1", "sm-oc", "k-t1").json()
    second = _accept("sm-t2", "sm-oc", "k-t2").json()
    both = _search(order_id="sm-oc",
                   created_from=first["created_at"],
                   created_to=second["created_at"]).json()
    assert set(_ids(both)) == {"sm-t1", "sm-t2"}
    only_first = _search(order_id="sm-oc", created_to=first["created_at"]).json()
    assert _ids(only_first) == ["sm-t1"]
    none = _search(order_id="sm-oc", created_from="9999-01-01T00:00:00+00:00").json()
    assert none["items"] == []


def test_search_ordering_stable_by_created_at_then_id():
    _make_order("sm-od")
    for index in range(5):
        _accept(f"sm-ord-{index}", "sm-od", f"k-ord-{index}")
    first = _search(order_id="sm-od").json()
    second = _search(order_id="sm-od").json()
    assert first == second
    keys = [(item["created_at"], item["movement_id"]) for item in first["items"]]
    assert keys == sorted(keys)


def test_search_pagination_covers_full_set_without_gaps_or_dupes():
    _make_order("sm-oe")
    expected = []
    for index in range(7):
        movement_id = f"sm-pg-{index}"
        _accept(movement_id, "sm-oe", f"k-pg-{index}")
        expected.append(movement_id)

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"order_id": "sm-oe", "limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        body = _search(**params).json()
        seen.extend(_ids(body))
        pages += 1
        if "next_cursor" not in body:
            break
        cursor = body["next_cursor"]
    assert pages == 3  # 3 + 3 + 1
    assert len(seen) == len(set(seen)) == 7
    assert set(seen) == set(expected)

    # The union of pages equals the unpaginated result set exactly.
    full = _search(order_id="sm-oe", limit=200).json()
    assert seen == _ids(full)


def test_search_last_page_has_no_cursor_and_empty_result_is_empty_list():
    _make_order("sm-of")
    _accept("sm-last-1", "sm-of", "k-last-1")
    body = _search(order_id="sm-of", limit=10).json()
    assert _ids(body) == ["sm-last-1"]
    assert "next_cursor" not in body
    empty = _search(order_id="sm-of", direction="out").json()
    assert empty == {"items": []}


def test_search_is_tenant_isolated():
    _make_order("sm-og")
    _accept("sm-iso-1", "sm-og", "k-iso-1")
    assert _ids(_search().json()) != []
    assert _search(tenant=OTHER).json() == {"items": []}


def test_search_invalid_params():
    assert _search(direction="sideways").status_code == 400
    assert _search(cursor="not-a-cursor").status_code == 400
    assert _search(limit=0).status_code == 422
    assert _search(quantity_min=0).status_code == 422
    no_tenant = client.get("/stock-movements")
    assert no_tenant.status_code == 400


def test_movements_do_not_change_order_or_refundable_quota():
    _make_order("sm-oh", amount=500)
    order_store.add_payment(TENANT, "sm-oh", 500)
    before = order_store.get(TENANT, "sm-oh")
    _accept("sm-nop-1", "sm-oh", "k-nop-1", direction="out", quantity=100)
    _accept("sm-nop-2", "sm-oh", "k-nop-2", direction="in", quantity=100)
    after = order_store.get(TENANT, "sm-oh")
    assert after == before
    # Refundable quota is untouched: a full-amount refund is still acceptable.
    refund = client.post(
        "/refunds",
        json={"refund_id": "sm-r1", "order_id": "sm-oh",
              "amount_cents": 500, "reason": "全额退款"},
        headers={"X-Tenant": TENANT, "Idempotency-Key": "k-r1"},
    )
    assert refund.status_code == 201
