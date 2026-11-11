"""Happy path + branching across all three configured workflows (same engine, different config)."""
from tests.conftest import submit


def test_loan_approved(client, loan):
    r = submit(client, "loan_approval", loan, "h1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == body["decision"] == "APPROVED"
    assert body["workflow_version"] == 1 and body["attempts"] == 1

    history = client.get(f"/requests/{body['request_id']}/history").json()
    assert [h["to_status"] for h in history] == ["RECEIVED", "IN_PROGRESS", "APPROVED"]


def test_loan_rejected_by_threshold_reports_all_failures(client, loan):
    loan["pan"] = "PQRSX5678K"  # bureau: score 590, 1 default
    body = submit(client, "loan_approval", loan).json()
    assert body["decision"] == "REJECTED"
    # Most severe first, but every failed rule is reported.
    assert body["reasons"][0].startswith("[CRD-001]") and "credit.score=590" in body["reasons"][0]
    assert any(r.startswith("[CRD-002]") for r in body["reasons"])


def test_loan_mandatory_check_short_circuits_before_dependency(client, container, loan):
    loan["employment_type"] = "unemployed"
    body = submit(client, "loan_approval", loan).json()
    assert body["decision"] == "REJECTED"
    assert container.gateway.service("credit_bureau").call_count == 0  # never called the bureau


def test_loan_conditional_branch_high_value_goes_to_manual_review(client, loan):
    loan.update(loan_amount=1500000, monthly_income=100000)
    body = submit(client, "loan_approval", loan).json()
    assert body["decision"] == "MANUAL_REVIEW"
    assert "10 lakh" in body["reasons"][0]


def test_loan_borderline_branch(client, loan):
    loan["pan"] = "LMNOP4321Z"  # score 680 with 2 active loans
    body = submit(client, "loan_approval", loan).json()
    assert body["decision"] == "MANUAL_REVIEW" and "Borderline" in body["reasons"][0]


def test_claim_approved_and_fraud_routing(client, claim):
    assert submit(client, "claim_processing", claim).json()["decision"] == "APPROVED"
    claim.update(claim_id="CLM-2", policy_id="POL-3003")  # fraud score 97
    assert submit(client, "claim_processing", claim).json()["decision"] == "REJECTED"
    claim.update(claim_id="CLM-3", policy_id="POL-2002")  # fraud score 85
    assert submit(client, "claim_processing", claim).json()["decision"] == "MANUAL_REVIEW"


def test_claim_missing_document_needs_manual_review(client, claim):
    claim["documents"] = ["claim_form"]
    body = submit(client, "claim_processing", claim).json()
    assert body["decision"] == "MANUAL_REVIEW" and "DOC-002" in body["reasons"][0]


def test_vendor_paths(client, vendor):
    assert submit(client, "vendor_approval", vendor).json()["decision"] == "APPROVED"
    blacklisted = {**vendor, "gstin": "29AABCT1332L1ZT"}
    assert submit(client, "vendor_approval", blacklisted).json()["decision"] == "REJECTED"
    uncertified = {**vendor, "category": "it_services", "has_iso_cert": False}
    assert submit(client, "vendor_approval", uncertified).json()["decision"] == "MANUAL_REVIEW"


def test_manual_review_completion(client, loan):
    loan.update(loan_amount=1500000, monthly_income=100000)
    rid = submit(client, "loan_approval", loan).json()["request_id"]
    r = client.post(f"/requests/{rid}/review", json={"decision": "APPROVED", "reviewer": "alice", "note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"
    hist = client.get(f"/requests/{rid}/history").json()
    assert hist[-1]["actor"] == "alice" and hist[-1]["from_status"] == "MANUAL_REVIEW"
    # Terminal: cannot be reviewed again.
    again = client.post(f"/requests/{rid}/review", json={"decision": "REJECTED", "reviewer": "bob"})
    assert again.status_code == 409


def test_review_rejected_when_not_awaiting_review(client, loan):
    rid = submit(client, "loan_approval", loan).json()["request_id"]  # APPROVED
    r = client.post(f"/requests/{rid}/review", json={"decision": "REJECTED", "reviewer": "bob"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state_transition"


def test_unknown_workflow_and_request(client, loan):
    assert submit(client, "nope", loan).status_code == 404
    assert client.get("/requests/req_missing").status_code == 404
