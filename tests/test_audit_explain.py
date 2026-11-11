"""Auditability and explainability."""
from tests.conftest import submit


def test_explanation_contains_rule_trace_and_data_reference(client, loan):
    loan["pan"] = "PQRSX5678K"
    rid = submit(client, "loan_approval", loan).json()["request_id"]
    exp = client.get(f"/requests/{rid}/explain").json()

    assert exp["decision"] == "REJECTED" and exp["workflow"] == "loan_approval v1"
    assert len(exp["config_hash"]) == 64                   # exact config used
    assert exp["input"]["pan"] == "PQRSX5678K"             # data reference: input
    assert exp["data_used"]["credit"]["score"] == 590      # data reference: external data
    text = " ".join(exp["steps"])
    for rule in ("ELG-001 PASS", "ELG-002 PASS", "ELG-003 PASS", "CRD-001 FAIL (REJECTED)", "CRD-003 PASS"):
        assert rule in text
    assert "credit.score=590 (required >= 650)" in text


def test_audit_log_is_complete_and_ordered(client, loan):
    client.post("/admin/dependencies/credit_bureau/faults", json={"mode": "fail_times", "count": 1})
    rid = submit(client, "loan_approval", loan, "audit-1").json()["request_id"]
    submit(client, "loan_approval", loan, "audit-1")  # replay
    audit = client.get(f"/requests/{rid}/audit").json()

    assert [e["event"] for e in audit["events"]] == [
        "REQUEST_RECEIVED", "PROCESSING_STARTED", "DEPENDENCY_CALL", "DECISION_MADE", "IDEMPOTENT_REPLAY"]
    received = audit["events"][0]["details"]
    assert received["idempotency_key"] == "audit-1" and received["workflow_version"] == 1
    trace = audit["decisions"][0]["trace"]
    assert [s["stage"] for s in trace] == ["eligibility", "credit_bureau_lookup", "credit_assessment", "risk_routing"]
    assert trace[1]["attempts"][0]["status"] == "error" and trace[1]["attempts"][1]["status"] == "ok"


def test_explain_includes_human_review(client, loan):
    loan.update(loan_amount=1500000, monthly_income=100000)
    rid = submit(client, "loan_approval", loan).json()["request_id"]
    client.post(f"/requests/{rid}/review", json={"decision": "REJECTED", "reviewer": "carol", "note": "doc mismatch"})
    exp = client.get(f"/requests/{rid}/explain").json()
    assert exp["decision"] == "REJECTED"
    assert exp["steps"][-1] == "Human review: Manual review by carol: doc mismatch"
    assert any("high_value_loan" in s for s in exp["steps"])  # machine trace preserved
