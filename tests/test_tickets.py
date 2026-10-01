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
from app.store.idempotency import now_iso

migrate()
client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
# Dedicated tenant so this module's fixtures never collide with the other suites.
TENANT = "tk"
H = {"X-Tenant": TENANT}

TICKET_FIELDS = {"ticket_id", "order_id", "issue", "status", "created_at", "updated_at"}


def _make_paid_order(order_id: str, amount: int = 1000, paid: int | None = None,
                     tenant: str = TENANT) -> None:
    order_store.insert(tenant, order_id, amount, "CNY")
    order_store.add_payment(tenant, order_id, paid if paid is not None else amount)


def _accept(ticket_id: str, order_id: str, key: str, issue: str = "货不对板",
            tenant: str = TENANT):
    return client.post(
        "/tickets",
        json={"ticket_id": ticket_id, "order_id": order_id, "issue": issue},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _process(ticket_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/tickets/{ticket_id}/process",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _resolve(ticket_id: str, key: str, note: str = "已补发", tenant: str = TENANT):
    return client.post(f"/tickets/{ticket_id}/resolve",
                       json={"resolution_note": note},
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _close(ticket_id: str, key: str, tenant: str = TENANT):
    return client.post(f"/tickets/{ticket_id}/close",
                       headers={"X-Tenant": tenant, "Idempotency-Key": key})


def _get(ticket_id: str, tenant: str = TENANT):
    return client.get(f"/tickets/{ticket_id}", headers={"X-Tenant": tenant})


def _reconcile(batch_id: str, order_id: str, key: str, tenant: str = TENANT,
               note: str = "月度对账"):
    return client.post(
        "/reconciliations",
        json={"batch_id": batch_id, "order_id": order_id, "note": note},
        headers={"X-Tenant": tenant, "Idempotency-Key": key},
    )


def _insert_pending_refund(refund_id: str, order_id: str, amount: int,
                           tenant: str = TENANT) -> None:
    """Insert a refund bypassing the quota guard to fabricate an over-occupied order."""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, reason, status, created_at) "
            "VALUES(?,?,?,?,?, 'pending', ?)",
            (tenant, refund_id, order_id, amount, "direct", now_iso()),
        )
    finally:
        conn.close()


def _insert_completed_batch(batch_id: str, order_id: str, conclusion: str,
                            tenant: str = TENANT) -> None:
    ts = now_iso()
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO reconciliations(tenant, batch_id, order_id, note, conclusion, "
            "status, created_at, completed_at) VALUES(?,?,?,?,?, 'completed', ?, ?)",
            (tenant, batch_id, order_id, "direct", conclusion, ts, ts),
        )
    finally:
        conn.close()


# ---------- accept & read ----------

def test_accept_ticket_is_pending_and_readable() -> None:
    _make_paid_order("tko1")
    resp = _accept("tk1-a", "tko1", "tk1-key", issue="少发一件")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == TICKET_FIELDS
    assert body["ticket_id"] == "tk1-a"
    assert body["order_id"] == "tko1"
    assert body["issue"] == "少发一件"
    assert body["status"] == "pending"
    assert body["created_at"] == body["updated_at"]

    got = _get("tk1-a")
    assert got.status_code == 200 and got.json() == body


def test_accept_missing_or_cross_tenant_order_is_not_found_without_leak() -> None:
    resp = _accept("tk2-a", "no-such-order", "tk2-key")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "ticket target not found"
    assert _get("tk2-a").status_code == 404

    _make_paid_order("foreign-o2", 500, tenant="t2")
    cross = _accept("tk2-b", "foreign-o2", "tk2-key-2", tenant="t1")
    assert cross.status_code == 404
    assert _get("tk2-b", tenant="t1").status_code == 404


def test_read_ticket_is_tenant_isolated() -> None:
    _make_paid_order("tko3")
    assert _accept("tk3-a", "tko3", "tk3-key").status_code == 201
    assert _get("tk3-a", tenant="t2").status_code == 404
    assert client.get("/tickets/tk3-a").status_code == 400


def test_duplicate_ticket_id_conflicts_and_keeps_first() -> None:
    _make_paid_order("tko4")
    first = _accept("tk4-a", "tko4", "tk4-key-a", issue="首次问题")
    assert first.status_code == 201
    dup = _accept("tk4-a", "tko4", "tk4-key-b", issue="另一个问题")
    assert dup.status_code == 409
    assert dup.json()["detail"] == "ticket already accepted"
    assert _get("tk4-a").json() == first.json()


def test_invalid_arguments_are_rejected() -> None:
    _make_paid_order("tko5")
    no_tenant = client.post("/tickets", json={"ticket_id": "tk5-a", "order_id": "tko5",
                                              "issue": "x"},
                            headers={"Idempotency-Key": "k"})
    assert no_tenant.status_code == 400
    no_key = client.post("/tickets", json={"ticket_id": "tk5-a", "order_id": "tko5",
                                           "issue": "x"}, headers=H)
    assert no_key.status_code == 400
    empty_issue = client.post("/tickets",
                              json={"ticket_id": "tk5-a", "order_id": "tko5", "issue": ""},
                              headers={**H, "Idempotency-Key": "k"})
    assert empty_issue.status_code == 422
    assert _get("tk5-a").status_code == 404


# ---------- state machine ----------

def test_process_moves_pending_to_processing_and_is_stable() -> None:
    _make_paid_order("tko6")
    _accept("tk6-a", "tko6", "tk6-acc")
    first = _process("tk6-a", "tk6-proc-a")
    assert first.status_code == 200 and first.json()["status"] == "processing"
    # processing -> process stays processing and flips nothing (updated_at fixed)
    second = _process("tk6-a", "tk6-proc-b")
    assert second.status_code == 200
    assert second.json() == first.json()
    assert _get("tk6-a").json() == first.json()


def test_resolve_records_note_and_close_is_terminal() -> None:
    _make_paid_order("tko7")
    _accept("tk7-a", "tko7", "tk7-acc")
    _process("tk7-a", "tk7-proc")
    resolved = _resolve("tk7-a", "tk7-res", note="补发并赔偿")
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    assert resolved.json()["updated_at"] >= resolved.json()["created_at"]

    conn = connect()
    try:
        note = conn.execute(
            "SELECT resolution_note FROM tickets WHERE ticket_id='tk7-a'").fetchone()
    finally:
        conn.close()
    assert note["resolution_note"] == "补发并赔偿"

    closed = _close("tk7-a", "tk7-close")
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    assert _close("tk7-a", "tk7-close-2").status_code == 409
    assert _get("tk7-a").json() == closed.json()


def test_resolve_pending_directly_then_close() -> None:
    _make_paid_order("tko8")
    _accept("tk8-a", "tko8", "tk8-acc")
    assert _resolve("tk8-a", "tk8-res").status_code == 200
    assert _get("tk8-a").json()["status"] == "resolved"
    assert _close("tk8-a", "tk8-close").status_code == 200


def test_close_allowed_from_every_non_terminal_state() -> None:
    _make_paid_order("tko9")
    _accept("tk9-a", "tko9", "tk9-acc-a")
    _accept("tk9-b", "tko9", "tk9-acc-b")
    _accept("tk9-c", "tko9", "tk9-acc-c")
    assert _close("tk9-a", "tk9-close-a").json()["status"] == "closed"  # from pending
    _process("tk9-b", "tk9-proc-b")
    assert _close("tk9-b", "tk9-close-b").json()["status"] == "closed"  # from processing
    _resolve("tk9-c", "tk9-res-c")
    assert _close("tk9-c", "tk9-close-c").json()["status"] == "closed"  # from resolved


def test_illegal_transitions_conflict_without_change() -> None:
    _make_paid_order("tko10")
    _accept("tk10-a", "tko10", "tk10-acc")
    _process("tk10-a", "tk10-proc")
    _resolve("tk10-a", "tk10-res")
    _close("tk10-a", "tk10-close")
    before = _get("tk10-a").json()

    proc = _process("tk10-a", "tk10-proc-again")
    assert proc.status_code == 409 and proc.json()["detail"].startswith("ticket in status closed")
    res = _resolve("tk10-a", "tk10-res-again")
    assert res.status_code == 409 and "closed" in res.json()["detail"]
    assert _get("tk10-a").json() == before

    # resolved (not yet closed) rejects process and a second resolve
    _accept("tk10-b", "tko10", "tk10-acc-b")
    _resolve("tk10-b", "tk10-res-b")
    assert _process("tk10-b", "tk10-proc-b").status_code == 409
    again = _resolve("tk10-b", "tk10-res-b-2")
    assert again.status_code == 409 and again.json()["detail"] == "ticket already resolved"
    assert _get("tk10-b").json()["status"] == "resolved"


def test_unknown_ticket_flips_return_not_found() -> None:
    assert _process("ghost-ticket", "k").status_code == 404
    assert _resolve("ghost-ticket", "k").status_code == 404
    assert _close("ghost-ticket", "k").status_code == 404


# ---------- reconciliation gate on resolve ----------

def test_mismatched_reconciliation_blocks_resolve_until_new_balanced_batch() -> None:
    _make_paid_order("tko11", 1000, paid=500)
    _accept("tk11-a", "tko11", "tk11-acc")
    # occupied (600) > paid (500) -> settled_balance negative -> mismatched
    _insert_pending_refund("tk11-rx", "tko11", 600)
    bad = _reconcile("tk11-b1", "tko11", "tk11-rec-1")
    assert bad.status_code == 201 and bad.json()["conclusion"] == "mismatched"

    blocked = _resolve("tk11-a", "tk11-res")
    assert blocked.status_code == 409
    assert blocked.json()["detail"].startswith("order has a mismatched reconciliation batch")
    assert _get("tk11-a").json()["status"] == "pending"  # nothing changed
    # process is unaffected by the gate
    assert _process("tk11-a", "tk11-proc").status_code == 200
    blocked2 = _resolve("tk11-a", "tk11-res-2")
    assert blocked2.status_code == 409
    assert _get("tk11-a").json()["status"] == "processing"

    # clear the anomaly, then re-reconcile under a NEW batch to balanced
    client.post("/refunds/tk11-rx/reverse", headers={**H, "Idempotency-Key": "tk11-rx-rev"})
    good = _reconcile("tk11-b2", "tko11", "tk11-rec-2")
    assert good.json()["conclusion"] == "balanced"
    resolved = _resolve("tk11-a", "tk11-res-3")
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"


def test_resolve_gate_uses_latest_completed_batch() -> None:
    _make_paid_order("tko12")
    _accept("tk12-a", "tko12", "tk12-acc")
    _insert_completed_batch("tk12-b1", "tko12", "mismatched")
    _insert_completed_batch("tk12-b2", "tko12", "balanced")
    assert _resolve("tk12-a", "tk12-res-a").status_code == 200

    _accept("tk12-b", "tko12", "tk12-acc-b")
    _insert_completed_batch("tk12-b3", "tko12", "mismatched")
    blocked = _resolve("tk12-b", "tk12-res-b")
    assert blocked.status_code == 409 and "mismatched" in blocked.json()["detail"]


def test_closed_ticket_is_exempt_from_reconciliation_gate() -> None:
    _make_paid_order("tko13")
    _accept("tk13-a", "tko13", "tk13-acc")
    _insert_completed_batch("tk13-b1", "tko13", "mismatched")
    closed = _close("tk13-a", "tk13-close")
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    # resolving stays a plain illegal-transition conflict, not the gate
    res = _resolve("tk13-a", "tk13-res")
    assert res.status_code == 409 and res.json()["detail"] == "ticket is closed and cannot be resolved"


def test_tickets_never_mutate_other_documents() -> None:
    _make_paid_order("tko14", 1000, paid=800)
    client.post("/refunds",
                json={"refund_id": "tk14-r1", "order_id": "tko14",
                      "amount_cents": 200, "reason": "x"},
                headers={**H, "Idempotency-Key": "tk14-r1"})
    client.post("/settlements",
                json={"settlement_id": "tk14-s1", "order_id": "tko14",
                      "amount_cents": 200, "refund_ids": ["tk14-r1"], "reason": "周期结算"},
                headers={**H, "Idempotency-Key": "tk14-s1-acc"})
    client.post("/settlements/tk14-s1/advance",
                headers={**H, "Idempotency-Key": "tk14-s1-adv"})
    batch = _reconcile("tk14-b1", "tko14", "tk14-rec")
    assert batch.status_code == 201

    before = {
        "order": client.get("/orders/tko14", headers=H).json(),
        "refund": client.get("/refunds/tk14-r1", headers=H).json(),
        "settlement": client.get("/settlements/tk14-s1", headers=H).json(),
        "batch": client.get("/reconciliations/tk14-b1", headers=H).json(),
    }
    _accept("tk14-a", "tko14", "tk14-acc")
    _process("tk14-a", "tk14-proc")
    _resolve("tk14-a", "tk14-res")
    _close("tk14-a", "tk14-close")
    after = {
        "order": client.get("/orders/tko14", headers=H).json(),
        "refund": client.get("/refunds/tk14-r1", headers=H).json(),
        "settlement": client.get("/settlements/tk14-s1", headers=H).json(),
        "batch": client.get("/reconciliations/tk14-b1", headers=H).json(),
    }
    assert before == after


# ---------- idempotent replay ----------

def test_replay_returns_first_result_and_records_once() -> None:
    _make_paid_order("tko15")
    a = _accept("tk15-a", "tko15", "same-acc-key")
    # lifecycle moves on afterwards, yet the replay returns the first pending view
    _process("tk15-a", "tk15-proc")
    _resolve("tk15-a", "tk15-res")
    b = _accept("tk15-a", "tko15", "same-acc-key")
    assert a.status_code == b.status_code == 201 and a.json() == b.json()

    p1 = _process("tk15-a", "same-proc-key")
    p2 = _process("tk15-a", "same-proc-key")
    assert p1.json() == p2.json()

    conn = connect()
    try:
        n_accept = conn.execute(
            "SELECT COUNT(*) c FROM idempotent_requests "
            "WHERE target_id='tk15-a' AND operation='ticket_accept'").fetchone()["c"]
        tickets = conn.execute(
            "SELECT COUNT(*) c FROM tickets WHERE ticket_id='tk15-a'").fetchone()["c"]
    finally:
        conn.close()
    assert n_accept == 1 and tickets == 1


def test_failed_accept_is_replayed_identically_then_succeeds_with_new_key() -> None:
    first = _accept("tk16-a", "tko16-ghost", "tk16-key")
    assert first.status_code == 404
    _make_paid_order("tko16-ghost", 500)
    replay = _accept("tk16-a", "tko16-ghost", "tk16-key")
    assert replay.status_code == 404 and replay.json() == first.json()
    assert _accept("tk16-a", "tko16-ghost", "tk16-key-retry").status_code == 201


def test_failed_flip_is_replayed_with_its_original_error() -> None:
    _make_paid_order("tko17")
    _accept("tk17-a", "tko17", "tk17-acc")
    _close("tk17-a", "tk17-close")
    r1 = _resolve("tk17-a", "stuck-key")
    r2 = _resolve("tk17-a", "stuck-key")
    assert r1.status_code == r2.status_code == 409
    assert r1.json() == r2.json()
    assert _get("tk17-a").json()["status"] == "closed"


def test_concurrent_different_fingerprints_single_winner() -> None:
    def race(op: str, make_ticket: str) -> tuple[str, list[tuple[int, str]]]:
        _make_paid_order(f"tko18-{op}")
        _accept(make_ticket, f"tko18-{op}", f"tk18-{op}-acc")
        results: list[tuple[int, str]] = []

        def run(i: int, barrier: threading.Barrier) -> None:
            barrier.wait()
            if op == "resolve":
                resp = _resolve(make_ticket, f"tk18-{op}-{i}")
            else:
                resp = _close(make_ticket, f"tk18-{op}-{i}")
            results.append((resp.status_code, resp.json().get("status", "")))

        barrier = threading.Barrier(8)
        threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return op, results

    expected = {"resolve": "resolved", "close": "closed"}
    for op, results in [race("resolve", "tk18-r"), race("close", "tk18-c")]:
        oks = [r for r in results if r[0] == 200]
        conflicts = [r for r in results if r[0] == 409]
        assert len(oks) == 1 and len(conflicts) == 7, (op, results)
        assert oks[0][1] == expected[op]
        assert all(r[1] == "" for r in conflicts)

    # each ticket recorded exactly one successful flip outcome beyond acceptance
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT target_id, operation FROM idempotent_requests "
            "WHERE target_id IN ('tk18-r','tk18-c') AND result_code='ok' "
            "ORDER BY target_id, operation").fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [
        ("tk18-c", "ticket_accept"), ("tk18-c", "ticket_close"),
        ("tk18-r", "ticket_accept"), ("tk18-r", "ticket_resolve"),
    ]


def test_concurrent_processes_flip_at_most_once() -> None:
    _make_paid_order("tko19")
    _accept("tk19-a", "tko19", "tk19-acc")
    stamps: list[str] = []
    barrier = threading.Barrier(6)

    def run(i: int, barrier: threading.Barrier) -> None:
        barrier.wait()
        resp = _process("tk19-a", f"tk19-proc-{i}")
        stamps.append(resp.json()["updated_at"])

    threads = [threading.Thread(target=run, args=(i, barrier)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _get("tk19-a").json()["status"] == "processing"
    assert set(stamps) == {_get("tk19-a").json()["updated_at"]}


# ---------- durability across restart ----------

def test_ticket_replay_survives_process_restart() -> None:
    db_path = os.path.join(tempfile.mkdtemp(), "restart-tickets.sqlite")
    env = {**os.environ, "APP_DB": db_path, "PYTHONPATH": str(ROOT)}
    script = (
        "import json, sys\n"
        "from app.store.db import migrate\n"
        "from app.store import orders, tickets\n"
        "migrate()\n"
        "if sys.argv[1] == 'first':\n"
        "    orders.insert('tk','to',1000,'CNY')\n"
        "    tickets.accept('tk','ta','to','货不对板','acc-key')\n"
        "    tickets.process('tk','ta','proc-key')\n"
        "out = tickets.resolve('tk','ta','已补发','res-key')\n"
        "print(json.dumps({'http': out.status, 'body': out.body}))\n"
    )
    first = subprocess.run([sys.executable, "-c", script, "first"], env=env,
                           cwd=ROOT, capture_output=True, text=True, check=False)
    second = subprocess.run([sys.executable, "-c", script, "second"], env=env,
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    a, b = json.loads(first.stdout.strip()), json.loads(second.stdout.strip())
    assert a == b and a["http"] == 200 and a["body"]["status"] == "resolved"
