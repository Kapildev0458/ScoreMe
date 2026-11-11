"""Duplicate requests and idempotency."""
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import submit


def test_same_key_same_payload_replays_without_reprocessing(client, container, loan):
    first = submit(client, "loan_approval", loan, "dup-1")
    calls_after_first = container.gateway.service("credit_bureau").call_count
    second = submit(client, "loan_approval", loan, "dup-1")

    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["request_id"] == first.json()["request_id"]
    assert second.json()["decision"] == first.json()["decision"]
    assert container.gateway.service("credit_bureau").call_count == calls_after_first  # no re-execution
    rid = first.json()["request_id"]
    assert len(container.repo.list_decisions(rid)) == 1
    assert any(e["event"] == "IDEMPOTENT_REPLAY" for e in client.get(f"/requests/{rid}/audit").json()["events"])


def test_same_key_different_payload_is_conflict(client, loan):
    submit(client, "loan_approval", loan, "dup-2")
    r = submit(client, "loan_approval", {**loan, "loan_amount": 600000}, "dup-2")
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_conflict"


def test_payload_key_order_does_not_matter(client, loan):
    a = submit(client, "loan_approval", loan, "dup-3")
    b = submit(client, "loan_approval", dict(reversed(list(loan.items()))), "dup-3")
    assert b.status_code == 200 and a.json()["request_id"] == b.json()["request_id"]


def test_no_key_identical_payload_is_deduplicated(client, loan):
    a = submit(client, "loan_approval", loan)
    b = submit(client, "loan_approval", loan)
    assert a.json()["request_id"] == b.json()["request_id"] and b.status_code == 200


def test_same_key_different_workflows_are_independent(client, loan, vendor):
    a = submit(client, "loan_approval", loan, "shared")
    b = submit(client, "vendor_approval", vendor, "shared")
    assert a.status_code == b.status_code == 201
    assert a.json()["request_id"] != b.json()["request_id"]


def test_concurrent_duplicates_create_exactly_one_request(client, container, loan):
    def fire(_):
        return submit(client, "loan_approval", loan, "race-1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(fire, range(8)))

    ok = [r for r in responses if r.status_code in (200, 201)]
    ids = {r.json()["request_id"] for r in ok}
    assert len(ids) == 1
    assert sum(r.status_code == 201 for r in responses) == 1
    # Any non-200/201 must be the explicit "in progress" signal, never a 5xx.
    assert all(r.status_code in (200, 201, 409) for r in responses)
    assert len(container.repo.list_decisions(ids.pop())) == 1
