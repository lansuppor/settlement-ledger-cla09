import os
import tempfile
import threading

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import order_imports as oi
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

# Dedicated tenant prefix so this module never collides with other test modules.
HEADER = "tenant,order_id,amount_cents,currency\n"


def _import(task_id: str, csv_text: str):
    return client.post("/order-imports", json={"task_id": task_id, "csv_content": csv_text})


def _csv(*rows: str) -> str:
    return HEADER + "\n".join(rows) + "\n"


def _order_ids(tenant: str) -> set[str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT order_id FROM orders WHERE tenant=?", (tenant,)).fetchall()
    finally:
        conn.close()
    return {r["order_id"] for r in rows}


def test_mixed_rows_independent_verdict_and_counts() -> None:
    csv_text = _csv(
        "it,o1,100,CNY",       # accept
        "it,o2,0,CNY",         # invalid amount
        "it,o3,50,XXX",        # unsupported currency
        "it,o4,300,USD",       # accept
        ",o5,10,CNY",          # missing tenant
    )
    resp = _import("imp-1", csv_text)
    assert resp.status_code == 201
    body = resp.json()
    assert body["task_id"] == "imp-1"
    assert body["status"] == "completed"
    assert body["total_rows"] == 5
    assert body["success_count"] == 2
    assert body["failure_count"] == 3
    assert body["processed_rows"] == 5
    assert body["created_at"]
    assert body["completed_at"]
    assert [e["line_no"] for e in body["errors"]] == [2, 3, 5]
    assert all(e["raw"] for e in body["errors"])
    assert _order_ids("it") == {"o1", "o4"}


def test_exact_and_order_id_duplicates_accepted_once() -> None:
    csv_text = _csv(
        "id,o1,100,CNY",  # first, accept
        "id,o1,100,CNY",  # exact duplicate
        "id,o1,200,CNY",  # same order_id, different content
        "id,o2,90,CNY",   # accept
    )
    body = _import("imp-dup", csv_text).json()
    assert body["success_count"] == 2
    assert body["failure_count"] == 2
    reasons = {e["line_no"]: e["reason"] for e in body["errors"]}
    assert reasons[2] == "duplicate row in import"
    assert reasons[3] == "duplicate order_id in import"
    assert _order_ids("id") == {"o1", "o2"}
    assert client.get("/orders/o1", headers={"X-Tenant": "id"}).json()["amount_cents"] == 100


def test_preexisting_order_is_failure_not_double_accept() -> None:
    client.post("/orders", json={"tenant": "pe", "order_id": "o1",
                                 "amount_cents": 500, "currency": "CNY"})
    body = _import("imp-pre", _csv("pe,o1,999,CNY", "pe,o2,100,CNY")).json()
    assert body["success_count"] == 1
    assert body["errors"][0]["line_no"] == 1
    assert body["errors"][0]["reason"] == "order already accepted"
    # Pre-existing order keeps its original amount; the import did not overwrite it.
    assert client.get("/orders/o1", headers={"X-Tenant": "pe"}).json()["amount_cents"] == 500


def test_replay_returns_identical_first_result() -> None:
    csv_text = _csv("rp,o1,100,CNY", "rp,o1,100,CNY", "rp,o2,0,CNY")
    first = _import("imp-replay", csv_text)
    assert first.status_code == 201
    second = _import("imp-replay", csv_text)
    assert second.status_code == 201
    assert second.json() == first.json()
    # No second acceptance: exactly one order in the tenant.
    assert _order_ids("rp") == {"o1"}


def test_same_task_id_different_content_conflicts_and_changes_nothing() -> None:
    first = _import("imp-conf", _csv("cf,o1,100,CNY"))
    assert first.status_code == 201
    # Sequential (non-concurrent) re-acceptance with different content.
    second = _import("imp-conf", _csv("cf,o2,200,CNY"))
    assert second.status_code == 409
    assert second.json()["detail"] == "import task already accepted with different content"
    # Existing task and orders are untouched.
    got = client.get("/order-imports/imp-conf").json()
    assert got["success_count"] == 1
    assert _order_ids("cf") == {"o1"}


def test_completed_task_replay_survives_restart() -> None:
    # The first result is reconstructed purely from persisted rows/orders; the
    # in-memory run registry being empty (as after a restart) must not matter.
    csv_text = _csv("rs,o1,100,CNY", "rs,o2,0,CNY")
    first = _import("imp-restart", csv_text).json()
    oi._RUNNING.clear()
    oi._RUN_LOCKS.clear()
    second = _import("imp-restart", csv_text)
    assert second.status_code == 201
    assert second.json() == first
    assert _order_ids("rs") == {"o1"}


def test_get_task_and_missing() -> None:
    _import("imp-get", _csv("gt,o1,100,CNY", "gt,o2,-5,CNY"))
    got = client.get("/order-imports/imp-get")
    assert got.status_code == 200
    body = got.json()
    assert body["total_rows"] == 2 and body["success_count"] == 1
    assert body["failure_count"] == 1 and body["processed_rows"] == 2
    assert client.get("/order-imports/does-not-exist").status_code == 404


def test_invalid_csv_is_400_and_leaves_no_task() -> None:
    bad_header = "a,b,c,d\nx,y,1,CNY\n"
    assert _import("imp-bad1", bad_header).status_code == 400
    assert _import("imp-bad2", "").status_code in (400, 422)
    assert client.get("/order-imports/imp-bad1").status_code == 404


def test_resume_after_crash_matches_one_shot_import() -> None:
    csv_text = _csv(
        "cr,o1,100,CNY",
        "cr,o1,200,CNY",  # order_id duplicate
        "cr,o2,0,CNY",    # invalid amount
        "cr,o3,70,EUR",
        "cr,o4,80,JPY",
        "cr,o5,90,GBP",   # unsupported currency
    )

    # Crash the run after the first two rows have committed; each row is its
    # own transaction, so earlier rows survive and the interrupted one is
    # rolled back wholesale (no half-row).
    real_process = oi._process_row
    calls = {"n": 0}

    def flaky(conn, task_id, plan):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated service interruption")
        return real_process(conn, task_id, plan)

    oi._process_row = flaky
    try:
        try:
            oi.submit("imp-crash", csv_text)
            assert False, "expected the interrupted run to raise"
        except RuntimeError:
            pass
    finally:
        oi._process_row = real_process

    mid = client.get("/order-imports/imp-crash").json()
    assert mid["status"] == "in_progress"
    assert mid["processed_rows"] == mid["success_count"] + mid["failure_count"]
    assert mid["processed_rows"] < mid["total_rows"]

    # Resume via the explicit run endpoint; it only processes outstanding rows.
    resumed = client.post("/order-imports/imp-crash/run")
    assert resumed.status_code == 200
    final = resumed.json()
    assert final["status"] == "completed"
    assert final["processed_rows"] == final["total_rows"] == 6
    assert final["success_count"] + final["failure_count"] == final["processed_rows"]

    # Each successful order accepted exactly once.
    assert _order_ids("cr") == {"o1", "o3", "o4"}

    # A fresh, uninterrupted import of the same content (isolated tenant so it
    # does not see the crashed task's orders) reaches an identical verdict
    # (counts + per-line errors), regardless of the interruption.
    reference_csv = csv_text.replace("cr,", "crr,")
    reference = _import("imp-crash-reference", reference_csv).json()
    assert final["success_count"] == reference["success_count"]
    assert final["failure_count"] == reference["failure_count"]
    assert (sorted((e["line_no"], e["reason"]) for e in final["errors"])
            == sorted((e["line_no"], e["reason"]) for e in reference["errors"]))

    # Submitting the same task again after recovery is still a pure replay.
    replay = _import("imp-crash", csv_text).json()
    assert replay["success_count"] == final["success_count"]
    assert replay["errors"] == final["errors"]
    assert _order_ids("cr") == {"o1", "o3", "o4"}


def test_resume_missing_task_is_404() -> None:
    assert client.post("/order-imports/nope/run").status_code == 404


def test_concurrent_same_content_all_get_first_result() -> None:
    csv_text = _csv("cc,o1,100,CNY", "cc,o1,100,CNY", "cc,o2,200,CNY")
    results: list[oi.Outcome] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(oi.submit("imp-conc-same", csv_text))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status == 201 for r in results)
    assert all(r.body == results[0].body for r in results)
    assert _order_ids("cc") == {"o1", "o2"}


def test_concurrent_different_content_only_one_takes_effect() -> None:
    outcomes: list[oi.Outcome] = []
    barrier = threading.Barrier(6)

    def worker(i: int) -> None:
        barrier.wait()
        outcomes.append(oi.submit("imp-conc-diff", _csv(f"cd{i},o{i},100,CNY")))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = [o for o in outcomes if o.status == 201]
    conflicts = [o for o in outcomes if o.status == 409]
    assert len(accepted) == 1
    assert len(conflicts) == 5
    # Concurrent racing submissions are distinguishable from sequential ones.
    assert all(o.code == "concurrent_conflict" for o in conflicts)
    # Exactly one tenant/order from the winning submission exists.
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(1) AS c FROM orders WHERE tenant LIKE 'cd%'").fetchone()["c"]
    finally:
        conn.close()
    assert count == 1


def test_imported_order_is_consistent_across_all_chains() -> None:
    tenant = "chain"
    _import("imp-chain", _csv(f"{tenant},o1,1000,CNY"))

    # Read + cross-tenant isolation.
    assert client.get("/orders/o1", headers={"X-Tenant": tenant}).status_code == 200
    assert client.get("/orders/o1", headers={"X-Tenant": "other"}).status_code == 404

    # Payment.
    paid = client.post("/orders/o1/payments", json={"amount_cents": 1000},
                       headers={"X-Tenant": tenant})
    assert paid.status_code == 200 and paid.json()["outstanding_cents"] == 0

    # Refund accept -> settlement accept/advance -> reconciliation.
    refund = client.post(
        "/refunds", json={"refund_id": "r1", "order_id": "o1",
                          "amount_cents": 300, "reason": "return"},
        headers={"X-Tenant": tenant, "Idempotency-Key": "imp-r1"})
    assert refund.status_code == 201
    settlement = client.post(
        "/settlements", json={"settlement_id": "s1", "order_id": "o1",
                              "amount_cents": 300, "refund_ids": ["r1"], "reason": "x"},
        headers={"X-Tenant": tenant, "Idempotency-Key": "imp-s1"})
    assert settlement.status_code == 201
    advanced = client.post("/settlements/s1/advance",
                           headers={"X-Tenant": tenant, "Idempotency-Key": "imp-a1"})
    assert advanced.status_code == 200 and advanced.json()["status"] == "effective"
    recon = client.post(
        "/reconciliations", json={"batch_id": "b1", "order_id": "o1", "note": "n"},
        headers={"X-Tenant": tenant, "Idempotency-Key": "imp-b1"})
    assert recon.status_code == 201 and recon.json()["conclusion"] == "balanced"

    # Ticket on the imported order, then resolve (latest reconciliation balanced).
    ticket = client.post(
        "/tickets", json={"ticket_id": "w1", "order_id": "o1", "issue": "issue"},
        headers={"X-Tenant": tenant, "Idempotency-Key": "imp-w1"})
    assert ticket.status_code == 201
    resolved = client.post(
        "/tickets/w1/resolve", json={"resolution_note": "done"},
        headers={"X-Tenant": tenant, "Idempotency-Key": "imp-wr1"})
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
