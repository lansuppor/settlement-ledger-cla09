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

EVENT_FIELDS = {"occurred_at", "operation", "status", "seq", "direction", "quantity"}


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


# ---------- accept / reverse leave their mark ----------

def test_accept_writes_single_accept_event() -> None:
    _make_order("smo1")
    accepted = _accept("sme1-a", "smo1", "sme1-key", direction="out", quantity=33)
    assert accepted.status_code == 201

    resp = _events("sme1-a")
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    event = events[0]
    assert set(event) == EVENT_FIELDS
    assert event["seq"] == 1
    assert event["operation"] == "accept"
    assert event["status"] == "accepted"
    assert event["direction"] == "out"
    assert event["quantity"] == 33
    assert event["occurred_at"] == accepted.json()["created_at"]


def test_reverse_appends_reverse_event_after_accept() -> None:
    _make_order("smo2")
    accepted = _accept("sme2-a", "smo2", "sme2-acc", direction="in", quantity=58).json()
    reversed_resp = _reverse("sme2-a", "sme2-rev")
    assert reversed_resp.status_code == 200

    events = _events("sme2-a").json()
    assert [e["seq"] for e in events] == [1, 2]
    assert [e["operation"] for e in events] == ["accept", "reverse"]

    first, second = events
    assert first["operation"] == "accept" and first["status"] == "accepted"
    assert first["direction"] == "in" and first["quantity"] == 58
    assert first["occurred_at"] == accepted["created_at"]

    # the reverse record keeps the accepted direction/quantity and the reversal time
    assert second["operation"] == "reverse" and second["status"] == "reversed"
    assert second["direction"] == "in" and second["quantity"] == 58
    assert second["seq"] == 2
    assert isinstance(second["occurred_at"], str) and second["occurred_at"]
    assert second["occurred_at"] >= first["occurred_at"]

    # trail agrees with the document read
    movement = client.get("/stock-movements/sme2-a", headers=H).json()
    assert movement["direction"] == second["direction"]
    assert movement["quantity"] == second["quantity"]
    assert movement["status"] == second["status"] == "reversed"


def test_events_are_ordered_occurred_at_then_seq() -> None:
    _make_order("smo3")
    _accept("sme3-a", "smo3", "sme3-acc", direction="out", quantity=7)
    _reverse("sme3-a", "sme3-rev")
    # force an occurred_at tie: ordering must then fall back to seq ASC
    conn = connect()
    try:
        tied = conn.execute(
            "SELECT MIN(occurred_at) t FROM stock_movement_events "
            "WHERE tenant=? AND movement_id='sme3-a'",
            (TENANT,),
        ).fetchone()["t"]
        conn.execute(
            "UPDATE stock_movement_events SET occurred_at=? "
            "WHERE tenant=? AND movement_id='sme3-a'",
            (tied, TENANT),
        )
    finally:
        conn.close()

    events = _events("sme3-a").json()
    assert [(e["occurred_at"], e["seq"]) for e in events] == sorted(
        (e["occurred_at"], e["seq"]) for e in events
    )
    assert [e["seq"] for e in events] == [1, 2]
    # repeated reads return the same stable result
    assert _events("sme3-a").json() == events


# ---------- failures, rejections and replays leave no mark ----------

def test_failed_and_rejected_requests_write_no_events() -> None:
    # accept against a missing order is 404 and leaves no movement/event
    assert _accept("sme4-ghost", "no-such-order", "sme4-key").status_code == 404
    assert _events("sme4-ghost").status_code == 404
    assert _event_count("sme4-ghost") == 0

    _make_order("smo4")
    assert _accept("sme4-a", "smo4", "sme4-acc", direction="in", quantity=10).status_code == 201
    # duplicate accept with a different fingerprint -> 409, no second accept event
    dup = _accept("sme4-a", "smo4", "sme4-other", direction="out", quantity=20)
    assert dup.status_code == 409
    assert _event_count("sme4-a") == 1

    # reverse of a missing movement and a double reverse are recorded errors,
    # but neither appends an event
    assert _reverse("sme4-missing", "sme4-rev-missing").status_code == 404
    assert _event_count("sme4-missing") == 0
    assert _reverse("sme4-a", "sme4-rev-a").status_code == 200
    again = _reverse("sme4-a", "sme4-rev-b")
    assert again.status_code == 409
    assert _event_count("sme4-a") == 2
    assert [e["operation"] for e in _events("sme4-a").json()] == ["accept", "reverse"]


def test_replays_do_not_append_events_or_double_count() -> None:
    _make_order("smo5")
    a1 = _accept("sme5-a", "smo5", "same-acc-key", direction="in", quantity=12)
    a2 = _accept("sme5-a", "smo5", "same-acc-key", direction="in", quantity=12)
    assert a1.status_code == a2.status_code == 201 and a1.json() == a2.json()
    assert _event_count("sme5-a") == 1

    r1 = _reverse("sme5-a", "same-rev-key")
    r2 = _reverse("sme5-a", "same-rev-key")
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    events = _events("sme5-a").json()
    assert _event_count("sme5-a") == 2
    assert [(e["seq"], e["operation"]) for e in events] == [(1, "accept"), (2, "reverse")]

    # a recorded 409 replay also adds nothing
    assert _reverse("sme5-a", "other-rev-key").status_code == 409
    assert _reverse("sme5-a", "other-rev-key").status_code == 409
    assert _event_count("sme5-a") == 2


def test_concurrent_distinct_fingerprints_single_winner_single_event() -> None:
    _make_order("smo6")
    results: list[int] = []
    barrier = threading.Barrier(8)

    def run(i: int) -> None:
        barrier.wait()
        results.append(_accept("sme6-a", "smo6", f"sme6-acc-{i}",
                               direction="in", quantity=i + 1).status_code)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1 and sorted(results).count(409) == 7
    accept_events = [e for e in _events("sme6-a").json() if e["operation"] == "accept"]
    assert len(accept_events) == 1 and accept_events[0]["seq"] == 1

    codes: list[int] = []
    barrier2 = threading.Barrier(8)

    def reverse_run(i: int) -> None:
        barrier2.wait()
        codes.append(_reverse("sme6-a", f"sme6-rev-{i}").status_code)

    threads = [threading.Thread(target=reverse_run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes).count(200) == 1 and sorted(codes).count(409) == 7
    events = _events("sme6-a").json()
    assert [(e["seq"], e["operation"]) for e in events] == [(1, "accept"), (2, "reverse")]
    assert _event_count("sme6-a") == 2


# ---------- tenant isolation, missing header, read-only ----------

def test_events_missing_cross_tenant_and_missing_header() -> None:
    assert _events("sme-never").status_code == 404
    assert client.get("/stock-movements/sme-never/events").status_code == 400

    _make_order("smo7")
    _accept("sme7-a", "smo7", "sme7-acc", direction="in", quantity=4)
    cross = _events("sme7-a", tenant="t2")
    assert cross.status_code == 404
    assert cross.json()["detail"] == "stock movement not found"
    # other tenant must see no rows at all
    conn = connect()
    try:
        leaked = conn.execute(
            "SELECT COUNT(*) c FROM stock_movement_events WHERE tenant='t2' AND movement_id='sme7-a'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert leaked == 0


def test_events_query_is_read_only() -> None:
    _make_order("smo8")
    _accept("sme8-a", "smo8", "sme8-acc", direction="in", quantity=50)
    _reverse("sme8-a", "sme8-rev")

    before = {
        "movement": client.get("/stock-movements/sme8-a", headers=H).json(),
        "events": _events("sme8-a").json(),
        "summary": client.get("/stock-movements/summary", headers=H).json(),
        "search": client.get("/stock-movements", headers=H,
                             params={"include_reversed": "true"}).json(),
    }
    for _ in range(3):
        assert _events("sme8-a").status_code == 200
    after = {
        "movement": client.get("/stock-movements/sme8-a", headers=H).json(),
        "events": _events("sme8-a").json(),
        "summary": client.get("/stock-movements/summary", headers=H).json(),
        "search": client.get("/stock-movements", headers=H,
                             params={"include_reversed": "true"}).json(),
    }
    assert before == after
    # events endpoint does not collide with the summary route
    assert client.get("/stock-movements/summary/events", headers=H).status_code == 404


def test_events_survive_process_restart_and_replay_still_appends_nothing() -> None:
    db_file = os.path.join(tempfile.mkdtemp(), "restart-stock-events.sqlite")
    env = {**os.environ, "APP_DB": db_file, "PYTHONPATH": str(ROOT)}
    script = (
        "import json, sys\n"
        "from app.store.db import migrate\n"
        "from app.store import orders, stock_movements\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('sme','sro',1000,'CNY')\n"
        "    stock_movements.accept('sme','sra','sro','in',42,'acc-key')\n"
        "    stock_movements.reverse('sme','sra','rev-key')\n"
        "# replays after restart return first results and append no events\n"
        "stock_movements.accept('sme','sra','sro','in',42,'acc-key')\n"
        "stock_movements.reverse('sme','sra','rev-key')\n"
        "print(json.dumps(stock_movements.events('sme','sra')))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b
    assert [(e["seq"], e["operation"], e["status"], e["direction"], e["quantity"])
            for e in a] == [
                (1, "accept", "accepted", "in", 42),
                (2, "reverse", "reversed", "in", 42),
            ]
