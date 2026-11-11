"""Partial-save failures: outcome must be all-or-nothing and recoverable."""
from tests.conftest import submit


def test_failed_outcome_save_rolls_back_and_is_recoverable(client, container, loan):
    container.repo.inject_commit_failure("outcome", times=1)
    r = submit(client, "loan_approval", loan, "persist-1")
    assert r.status_code == 503 and r.json()["error"]["code"] == "persistence_failure"
    rid = r.json()["error"]["details"]["request_id"]

    # No partial outcome visible: no decision row, no DECISION_MADE / DEPENDENCY_CALL audit.
    assert container.repo.list_decisions(rid) == []
    events = [e["event"] for e in client.get(f"/requests/{rid}/audit").json()["events"]]
    assert "DECISION_MADE" not in events and "DEPENDENCY_CALL" not in events
    assert events[-1] == "PROCESSING_FAILED"
    assert client.get(f"/requests/{rid}").json()["status"] == "ERROR"

    # Client safely retries with the same idempotency key -> resumes the same request.
    again = submit(client, "loan_approval", loan, "persist-1")
    assert again.status_code == 201 and again.json()["request_id"] == rid
    assert again.json()["status"] == "APPROVED"
    assert len(container.repo.list_decisions(rid)) == 1


def test_error_state_can_be_retried_via_endpoint(client, container, loan):
    container.repo.inject_commit_failure("outcome", times=1)
    rid = submit(client, "loan_approval", loan).json()["error"]["details"]["request_id"]
    r = client.post(f"/requests/{rid}/retry")
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"


def test_failed_intake_leaves_no_trace_and_key_is_reusable(client, container, loan):
    container.repo.inject_commit_failure("intake", times=1)
    assert submit(client, "loan_approval", loan, "persist-2").status_code == 503
    assert container.repo.list_requests() == []
    ok = submit(client, "loan_approval", loan, "persist-2")
    assert ok.status_code == 201 and ok.json()["decision"] == "APPROVED"
