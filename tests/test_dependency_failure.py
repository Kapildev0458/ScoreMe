"""External dependency failures, retries with backoff and circuit breaking."""
from app.configuration.models import RetryPolicy
from app.services.dependencies import DependencyGateway, MockCreditBureau, compute_backoff
from tests.conftest import submit


def set_fault(client, name, mode, count=0):
    r = client.post(f"/admin/dependencies/{name}/faults", json={"mode": mode, "count": count})
    assert r.status_code == 200


def test_transient_failure_recovered_by_retry(client, sleeps, loan):
    set_fault(client, "credit_bureau", "fail_times", count=2)
    body = submit(client, "loan_approval", loan).json()
    assert body["decision"] == "APPROVED"

    audit = client.get(f"/requests/{body['request_id']}/audit").json()
    call = next(e for e in audit["events"] if e["event"] == "DEPENDENCY_CALL")["details"]
    assert [a["status"] for a in call["attempts"]] == ["error", "error", "ok"]
    # Exponential backoff from config (base 200ms, x2, with jitter in [50%,100%]).
    assert len(sleeps) == 2
    assert 0.1 <= sleeps[0] <= 0.2 and 0.2 <= sleeps[1] <= 0.4


def test_outage_parks_request_as_retry_pending(client, loan):
    set_fault(client, "credit_bureau", "fail_always")
    body = submit(client, "loan_approval", loan).json()
    assert body["status"] == "RETRY_PENDING"
    assert "after 3 attempt(s)" in body["reasons"][0]


def test_timeout_is_handled_like_outage(client, loan):
    set_fault(client, "credit_bureau", "timeout_always")
    body = submit(client, "loan_approval", loan).json()
    assert body["status"] == "RETRY_PENDING" and "DependencyTimeout" in body["reasons"][0]


def test_fail_safe_policy_for_money_movement(client, claim):
    """Claim workflow is configured to go to MANUAL_REVIEW (never auto-approve) when fraud check is down."""
    set_fault(client, "fraud_check", "fail_always")
    body = submit(client, "claim_processing", claim).json()
    assert body["decision"] == "MANUAL_REVIEW" and "Fraud service unavailable" in body["reasons"][0]


def test_non_retryable_error_is_not_retried():
    gw = DependencyGateway([MockCreditBureau()], sleep=lambda s: None)
    out = gw.call("credit_bureau", {"pan": None}, RetryPolicy(max_attempts=5))
    assert not out.ok and len(out.attempts) == 1 and "DependencyBadRequest" in out.error


def test_circuit_breaker_opens_and_fails_fast():
    svc = MockCreditBureau()
    svc.faults.configure("fail_always")
    gw = DependencyGateway([svc], sleep=lambda s: None, breaker_threshold=3)
    gw.call("credit_bureau", {"pan": "ABCDE1234F"}, RetryPolicy(max_attempts=3))
    assert gw.breaker("credit_bureau").state == "open"
    calls_before = svc.call_count
    out = gw.call("credit_bureau", {"pan": "ABCDE1234F"}, RetryPolicy(max_attempts=3))
    assert out.attempts == [{"attempt": 1, "status": "circuit_open"}]
    assert svc.call_count == calls_before  # did not hit the failing service at all


def test_circuit_breaker_half_open_recovers():
    now = [0.0]
    svc = MockCreditBureau()
    svc.faults.configure("fail_always")
    gw = DependencyGateway([svc], sleep=lambda s: None, breaker_threshold=1, breaker_reset_s=10)
    gw._breakers["credit_bureau"]._clock = lambda: now[0]
    gw.call("credit_bureau", {"pan": "ABCDE1234F"}, RetryPolicy(max_attempts=1))
    assert gw.breaker("credit_bureau").state == "open"
    now[0] = 11
    assert gw.breaker("credit_bureau").state == "half_open"
    svc.faults.configure("none")
    assert gw.call("credit_bureau", {"pan": "ABCDE1234F"}, RetryPolicy(max_attempts=1)).ok
    assert gw.breaker("credit_bureau").state == "closed"


def test_backoff_is_capped():
    p = RetryPolicy(max_attempts=10, base_delay_ms=100, max_delay_ms=500, multiplier=3)
    assert compute_backoff(p, 8) <= 0.5
