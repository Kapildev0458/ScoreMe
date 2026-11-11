"""Request-level retry flow (RETRY_PENDING -> retry -> decision) and retry budget."""
from tests.conftest import submit


def fault(client, mode):
    client.post("/admin/dependencies/credit_bureau/faults", json={"mode": mode})


def test_retry_after_dependency_recovers(client, loan):
    fault(client, "fail_always")
    rid = submit(client, "loan_approval", loan, "retry-1").json()["request_id"]
    assert client.get(f"/requests/{rid}").json()["status"] == "RETRY_PENDING"

    fault(client, "none")
    r = client.post(f"/requests/{rid}/retry")
    assert r.status_code == 200 and r.json()["status"] == "APPROVED" and r.json()["attempts"] == 2

    statuses = [h["to_status"] for h in client.get(f"/requests/{rid}/history").json()]
    assert statuses == ["RECEIVED", "IN_PROGRESS", "RETRY_PENDING", "IN_PROGRESS", "APPROVED"]
    decisions = client.get(f"/requests/{rid}/audit").json()["decisions"]
    assert [d["decision"] for d in decisions] == ["RETRY_PENDING", "APPROVED"]  # full history kept


def test_duplicate_submit_of_retry_pending_does_not_reprocess(client, container, loan):
    fault(client, "fail_always")
    first = submit(client, "loan_approval", loan, "retry-2").json()
    again = submit(client, "loan_approval", loan, "retry-2")
    assert again.status_code == 200 and again.json()["status"] == "RETRY_PENDING"
    assert len(container.repo.list_decisions(first["request_id"])) == 1


def test_retry_budget_exhaustion_escalates_to_manual_review(client, loan):
    fault(client, "fail_always")
    rid = submit(client, "loan_approval", loan).json()["request_id"]           # attempt 1
    assert client.post(f"/requests/{rid}/retry").json()["status"] == "RETRY_PENDING"  # attempt 2
    final = client.post(f"/requests/{rid}/retry").json()                         # attempt 3 = max
    assert final["status"] == "MANUAL_REVIEW"
    assert any("Retry budget exhausted" in r for r in final["reasons"])


def test_retry_not_allowed_on_completed_request(client, loan):
    rid = submit(client, "loan_approval", loan).json()["request_id"]
    r = client.post(f"/requests/{rid}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state_transition"
