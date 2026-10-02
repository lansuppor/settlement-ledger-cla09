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
TENANT = "sme"
H = {"X-Tenant": TENANT}

EVENT_FIELDS = {"seq", "action", "status", "direction", "quantity", "occurred_at"}


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


def _events(movement_id: str, tenant: str = TENANT):
    return client.get(f"/stock-movements/{movement_id}/events",
                      headers={"X-Tenant": tenant})


def _event_count(movement_id: str, tenant: str = TENANT) -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) c FROM stock_movement_events WHERE tenant=? AND movement_id=?",
            (tenant, movement_id),
        ).fetchone()["c"]
    finally:
        conn.close()


# ---------- accept trail ----------

def test_accept_leaves_one_event_with_accepted_values() -> None:
    _make_order("smeo1")
    accepted = _accept("sme1-a", "smeo1", "sme1-acc", direction="out", quantity=77)
    assert accepted.status_code == 201

    resp = _events("sme1-a")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    event = items[0]
    assert set(event) == EVENT_FIELDS
    assert event["seq"] == 1
    assert event["action"] == "accept"
    assert event["status"] == "accepted"
    assert event["direction"] == "out"
    assert event["quantity"] == 77
    assert event["occurred_at"] == accepted.json()["created_at"]


def test_reverse_appends_second_event_keeping_accepted_values() -> None:
    _make_order("smeo2")
    accepted = _accept("sme2-a", "smeo2", "sme2-acc", direction="in", quantity=123)
    assert accepted.status_code == 201
    assert _reverse("sme2-a", "sme2-rev").status_code == 200

    items = _events("sme2-a").json()["items"]
    assert [e["seq"] for e in items] == [1, 2]
    assert [e["action"] for e in items] == ["accept", "reverse"]
    assert [e["status"] for e in items] == ["accepted", "reversed"]
    # the reverse event keeps the accepted direction/quantity verbatim
    assert items[1]["direction"] == "in" and items[1]["quantity"] == 123
    assert items[0]["occurred_at"] == accepted.json()["created_at"]
    assert items[1]["occurred_at"] >= items[0]["occurred_at"]


def test_events_are_stable_across_repeated_queries() -> None:
    _make_order("smeo3")
    _accept("sme3-a", "smeo3", "sme3-acc", direction="in", quantity=5)
    _reverse("sme3-a", "sme3-rev")
    first = _events("sme3-a")
    again = _events("sme3-a")
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()


# ---------- tenant isolation & missing ----------

def test_events_missing_or_cross_tenant_is_404_without_leak() -> None:
    assert _events("sme4-ghost").status_code == 404

    _make_order("smeo4")
    _accept("sme4-a", "smeo4", "sme4-acc")
    cross = _events("sme4-a", tenant="t2")
    assert cross.status_code == 404
    assert cross.json()["detail"] == "stock movement not found"
    # owning tenant still reads the trail
    assert len(_events("sme4-a").json()["items"]) == 1


def test_events_require_tenant_header() -> None:
    assert client.get("/stock-movements/whatever/events").status_code == 400


# ---------- replay & failure semantics ----------

def test_accept_replay_appends_no_event() -> None:
    _make_order("smeo5")
    a = _accept("sme5-a", "smeo5", "sme5-same-key", direction="in", quantity=11)
    b = _accept("sme5-a", "smeo5", "sme5-same-key", direction="out", quantity=22)
    assert a.status_code == b.status_code == 201 and a.json() == b.json()
    items = _events("sme5-a").json()["items"]
    assert len(items) == 1
    assert items[0]["direction"] == "in" and items[0]["quantity"] == 11
    assert _event_count("sme5-a") == 1


def test_failed_accept_leaves_no_event() -> None:
    first = _accept("sme6-a", "sme6-ghost", "sme6-key")
    assert first.status_code == 404
    assert _events("sme6-a").status_code == 404

    _make_order("sme6-ghost", 500)
    replay = _accept("sme6-a", "sme6-ghost", "sme6-key")
    assert replay.status_code == 404
    assert _events("sme6-a").status_code == 404

    # a fresh fingerprint succeeds and only then leaves the accept event
    assert _accept("sme6-a", "sme6-ghost", "sme6-key-retry",
                   direction="out", quantity=7).status_code == 201
    items = _events("sme6-a").json()["items"]
    assert len(items) == 1 and items[0]["action"] == "accept"
    assert items[0]["direction"] == "out" and items[0]["quantity"] == 7


def test_conflicting_accept_leaves_no_extra_event() -> None:
    _make_order("smeo7")
    _accept("sme7-a", "smeo7", "sme7-key-a", direction="in", quantity=10)
    dup = _accept("sme7-a", "smeo7", "sme7-key-b", direction="out", quantity=20)
    assert dup.status_code == 409
    items = _events("sme7-a").json()["items"]
    assert len(items) == 1
    assert items[0]["direction"] == "in" and items[0]["quantity"] == 10


def test_reverse_replay_and_failed_reverse_append_no_event() -> None:
    _make_order("smeo8")
    _accept("sme8-a", "smeo8", "sme8-acc", direction="out", quantity=40)

    # replayed reverse: one reverse event total
    a = _reverse("sme8-a", "sme8-same-rev-key")
    b = _reverse("sme8-a", "sme8-same-rev-key")
    assert a.status_code == b.status_code == 200
    assert _event_count("sme8-a") == 2

    # rejected reverse (already reversed) adds nothing
    assert _reverse("sme8-a", "sme8-rev-dup").status_code == 409
    items = _events("sme8-a").json()["items"]
    assert [e["action"] for e in items] == ["accept", "reverse"]
    assert _event_count("sme8-a") == 2


def test_failed_reverse_404_leaves_no_event() -> None:
    assert _reverse("sme9-a", "sme9-rev").status_code == 404
    assert _events("sme9-a").status_code == 404

    _make_order("smeo9")
    _accept("sme9-a", "smeo9", "sme9-acc", direction="in", quantity=9)
    # replayed 404 still leaves nothing new
    assert _reverse("sme9-a", "sme9-rev").status_code == 404
    assert _event_count("sme9-a") == 1
    # a fresh key reverses and appends exactly one event
    assert _reverse("sme9-a", "sme9-rev-retry").status_code == 200
    items = _events("sme9-a").json()["items"]
    assert [e["seq"] for e in items] == [1, 2]
    assert [e["action"] for e in items] == ["accept", "reverse"]


# ---------- concurrency ----------

def test_concurrent_accepts_single_winner_single_event() -> None:
    _make_order("smeo10")
    results: list[int] = []
    barrier = threading.Barrier(8)

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        results.append(
            _accept("sme10-a", "smeo10", f"sme10-key-{i}", direction="in",
                    quantity=i + 1).status_code
        )

    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 7
    items = _events("sme10-a").json()["items"]
    assert len(items) == 1 and items[0]["action"] == "accept"
    assert _event_count("sme10-a") == 1


def test_concurrent_reverses_single_winner_single_event() -> None:
    _make_order("smeo11")
    _accept("sme11-a", "smeo11", "sme11-acc", direction="in", quantity=50)
    codes: list[int] = []
    barrier = threading.Barrier(8)

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        codes.append(_reverse("sme11-a", f"sme11-rev-{i}").status_code)

    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1
    assert sorted(codes).count(409) == 7
    items = _events("sme11-a").json()["items"]
    assert [e["action"] for e in items] == ["accept", "reverse"]
    assert _event_count("sme11-a") == 2


# ---------- restart ----------

def test_events_survive_process_restart_and_replay_appends_nothing() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "restart-stock-events.sqlite")
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    script = (
        "import json, sys\n"
        "from app.store.db import migrate\n"
        "from app.store import orders, stock_movements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('sme','smeo',1000,'CNY')\n"
        "    stock_movements.accept('sme','smea','smeo','in',42,'sme-acc-key')\n"
        "else:\n"
        "    stock_movements.accept('sme','smea','smeo','in',42,'sme-acc-key')\n"
        "    stock_movements.reverse('sme','smea','sme-rev-key')\n"
        "    stock_movements.reverse('sme','smea','sme-rev-key')\n"
        "print(json.dumps(stock_movements.events('sme','smea')))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    before = json.loads(first.stdout.strip())
    after = json.loads(second.stdout.strip())
    # first run: only the accept event; second run replays accept, reverses once
    # (the second reverse is a replay) — exactly one event per effective action
    assert [e["action"] for e in before] == ["accept"]
    assert [e["action"] for e in after] == ["accept", "reverse"]
    assert [e["seq"] for e in after] == [1, 2]
    assert after[0] == before[0]  # the accept event is never rewritten
    assert after[1]["direction"] == "in" and after[1]["quantity"] == 42


# ---------- read-only & consistency ----------

def test_events_query_is_read_only_and_matches_movement() -> None:
    _make_order("smeo12", 1000)
    _accept("sme12-a", "smeo12", "sme12-acc", direction="out", quantity=30)
    _reverse("sme12-a", "sme12-rev")

    movement_before = client.get("/stock-movements/sme12-a", headers=H).json()
    items = _events("sme12-a").json()["items"]
    movement_after = client.get("/stock-movements/sme12-a", headers=H).json()
    assert movement_before == movement_after

    # the trail agrees with the movement read: final status and verbatim values
    assert items[-1]["status"] == movement_after["status"] == "reversed"
    for event in items:
        assert event["direction"] == movement_after["direction"]
        assert event["quantity"] == movement_after["quantity"]

    # event count equals the number of effective actions (accept + reverse)
    assert _event_count("sme12-a") == 2


def test_events_do_not_touch_other_documents() -> None:
    _make_order("smeo13", 1000)
    order_store.add_payment(TENANT, "smeo13", 800)
    client.post("/refunds",
                json={"refund_id": "sme13-r1", "order_id": "smeo13",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "sme13-r1"})
    before = {
        "order": client.get("/orders/smeo13", headers=H).json(),
        "refund": client.get("/refunds/sme13-r1", headers=H).json(),
    }
    _accept("sme13-m1", "smeo13", "sme13-acc", direction="in", quantity=60)
    _reverse("sme13-m1", "sme13-rev")
    assert _events("sme13-m1").status_code == 200
    after = {
        "order": client.get("/orders/smeo13", headers=H).json(),
        "refund": client.get("/refunds/sme13-r1", headers=H).json(),
    }
    assert before == after
    # refundable conservation still holds: paid 800 - pending 200 = 600
    assert client.post("/refunds",
                       json={"refund_id": "sme13-r2", "order_id": "smeo13",
                             "amount_cents": 600, "reason": "x"},
                       headers={**H, "Idempotency-Key": "sme13-r2"}).status_code == 201
