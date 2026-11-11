"""Schema validation at intake."""
from tests.conftest import submit


def test_all_errors_reported_at_once(client, loan):
    loan.update(age="thirty", pan="bad", loan_amount=5)
    del loan["employment_type"]
    r = submit(client, "loan_approval", loan)
    assert r.status_code == 422
    errors = {e["field"]: e["error"] for e in r.json()["error"]["details"]["errors"]}
    assert errors == {
        "age": "must be of type integer",
        "pan": "must match pattern ^[A-Z]{5}[0-9]{4}[A-Z]$",
        "loan_amount": "must be >= 10000",
        "employment_type": "is required",
    }


def test_enum_bool_and_array_types(client, vendor, claim):
    vendor.update(category="weapons", has_iso_cert="yes")
    errs = submit(client, "vendor_approval", vendor).json()["error"]["details"]["errors"]
    assert {e["field"] for e in errs} == {"category", "has_iso_cert"}
    claim["documents"] = "claim_form"
    errs = submit(client, "claim_processing", claim).json()["error"]["details"]["errors"]
    assert errs == [{"field": "documents", "error": "must be of type array"}]


def test_boolean_is_not_accepted_as_integer(client, loan):
    loan["age"] = True
    assert submit(client, "loan_approval", loan).status_code == 422


def test_non_object_body_rejected(client):
    r = client.post("/workflows/loan_approval/requests", json=[1, 2, 3])
    assert r.status_code == 422


def test_invalid_request_is_audited_and_does_not_consume_idempotency_key(client, loan):
    bad = {**loan, "age": 5}
    r = submit(client, "loan_approval", bad, key="same-key")
    rid = r.json()["error"]["details"]["request_id"]
    audit = client.get(f"/requests/{rid}/audit").json()
    assert audit["events"][0]["event"] == "VALIDATION_FAILED"
    assert client.get(f"/requests/{rid}").json()["status"] == "INVALID"
    # Client fixes the payload and reuses the same key: must be processed, not a 409.
    ok = submit(client, "loan_approval", loan, key="same-key")
    assert ok.status_code == 201 and ok.json()["decision"] == "APPROVED"


def test_rule_failure_is_not_input_failure(client, loan):
    """Business-rule rejection (valid input) returns 201 + REJECTED, not 422."""
    loan["age"] = 19  # schema allows >=18, rule ELG-001 requires 21
    r = submit(client, "loan_approval", loan)
    assert r.status_code == 201 and r.json()["decision"] == "REJECTED"
