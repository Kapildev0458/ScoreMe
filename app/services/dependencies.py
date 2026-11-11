"""Simulated external dependencies plus the resilience layer around them.

* ``MockCreditBureau`` / ``MockFraudCheck`` / ``MockBusinessRegistry`` stand in
  for third-party APIs. Responses are deterministic (fixture table + hash
  fallback) so tests and demos are reproducible.
* ``FaultInjector`` makes any service fail on demand (always / N times /
  timeout / random rate) to exercise failure handling.
* ``CircuitBreaker`` stops hammering a dependency that is clearly down.
* ``DependencyGateway`` is the only thing the workflow engine talks to. It
  applies the retry policy from config (exponential backoff + jitter) and
  returns a structured outcome with every attempt recorded for the audit log.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app.configuration.models import RetryPolicy


# --------------------------------------------------------------------------- errors
class DependencyError(Exception):
    retryable = True


class DependencyTimeout(DependencyError):
    pass


class DependencyUnavailable(DependencyError):
    pass


class DependencyBadRequest(DependencyError):
    retryable = False  # retrying a bad request will never succeed


class CircuitOpenError(DependencyError):
    retryable = False


# --------------------------------------------------------------------------- fault injection
class FaultInjector:
    MODES = ("none", "fail_always", "fail_times", "timeout_always", "random")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode = "none"
        self.remaining = 0
        self.failure_rate = 0.0
        self._rng = random.Random(42)

    def configure(self, mode: str, count: int = 0, failure_rate: float = 0.0) -> None:
        if mode not in self.MODES:
            raise ValueError(f"unknown fault mode '{mode}', expected one of {self.MODES}")
        with self._lock:
            self.mode, self.remaining, self.failure_rate = mode, count, failure_rate

    def check(self) -> None:
        with self._lock:
            if self.mode == "fail_always":
                raise DependencyUnavailable("simulated outage (503)")
            if self.mode == "timeout_always":
                raise DependencyTimeout("simulated timeout")
            if self.mode == "fail_times" and self.remaining > 0:
                self.remaining -= 1
                raise DependencyUnavailable(f"simulated transient failure ({self.remaining} more queued)")
            if self.mode == "random" and self._rng.random() < self.failure_rate:
                raise DependencyUnavailable("simulated random failure")

    def snapshot(self) -> dict[str, Any]:
        return {"mode": self.mode, "remaining": self.remaining, "failure_rate": self.failure_rate}


# --------------------------------------------------------------------------- mock services
class ExternalService(Protocol):
    name: str
    faults: FaultInjector

    def call(self, params: dict[str, Any]) -> dict[str, Any]: ...


def _stable_int(text: str, modulo: int) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16) % modulo


class _MockService:
    name = "mock"

    def __init__(self, latency_ms: int = 0) -> None:
        self.faults = FaultInjector()
        self.latency_ms = latency_ms
        self.call_count = 0

    def call(self, params: dict[str, Any]) -> dict[str, Any]:
        self.call_count += 1
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        self.faults.check()
        return self._respond(params)

    def _respond(self, params: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class MockCreditBureau(_MockService):
    """Credit bureau lookup by PAN."""

    name = "credit_bureau"
    FIXTURES = {
        "ABCDE1234F": {"score": 780, "defaults_last_24m": 0, "active_loans": 1},
        "PQRSX5678K": {"score": 590, "defaults_last_24m": 1, "active_loans": 4},
        "LMNOP4321Z": {"score": 680, "defaults_last_24m": 0, "active_loans": 2},
        "DEFAU1111D": {"score": 720, "defaults_last_24m": 2, "active_loans": 3},
    }

    def _respond(self, params: dict[str, Any]) -> dict[str, Any]:
        pan = params.get("pan")
        if not pan:
            raise DependencyBadRequest("pan is required")
        data = self.FIXTURES.get(pan) or {
            "score": 300 + _stable_int(pan, 601),
            "defaults_last_24m": _stable_int(pan + "d", 3),
            "active_loans": _stable_int(pan + "l", 5),
        }
        return {"pan": pan, "bureau": "MockBureau", **data}


class MockFraudCheck(_MockService):
    """Fraud scoring for insurance claims (0 = clean, 100 = certain fraud)."""

    name = "fraud_check"
    FIXTURES = {"POL-1001": 12, "POL-2002": 85, "POL-3003": 97}

    def _respond(self, params: dict[str, Any]) -> dict[str, Any]:
        policy = params.get("policy_id")
        if not policy:
            raise DependencyBadRequest("policy_id is required")
        score = self.FIXTURES.get(policy, _stable_int(policy, 60))
        return {"risk_score": score, "model": "mock-fraud-v1", "signals": ["high_claim_ratio"] if score >= 80 else []}


class MockBusinessRegistry(_MockService):
    """GST / business registry lookup for vendor onboarding."""

    name = "business_registry"
    FIXTURES = {
        "27AAPFU0939F1ZV": {"gst_status": "active", "blacklisted": False, "legal_name": "Acme Supplies LLP"},
        "29AABCT1332L1ZT": {"gst_status": "active", "blacklisted": True, "legal_name": "Shady Traders Pvt Ltd"},
        "07AAACC1206D1ZM": {"gst_status": "cancelled", "blacklisted": False, "legal_name": "Closed Co Pvt Ltd"},
    }

    def _respond(self, params: dict[str, Any]) -> dict[str, Any]:
        gstin = params.get("gstin")
        if not gstin:
            raise DependencyBadRequest("gstin is required")
        return {"gstin": gstin, **self.FIXTURES.get(gstin, {"gst_status": "active", "blacklisted": False, "legal_name": None})}


# --------------------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    """Classic closed -> open -> half-open breaker (per dependency)."""

    def __init__(self, failure_threshold: int = 5, reset_timeout_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._lock = threading.Lock()
        self.consecutive_failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self._clock() - self.opened_at >= self.reset_timeout_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold or self.state == "half_open":
                self.opened_at = self._clock()

    def reset(self) -> None:
        self.record_success()


# --------------------------------------------------------------------------- gateway
@dataclass
class CallOutcome:
    service: str
    ok: bool
    value: dict[str, Any] | None = None
    error: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"service": self.service, "ok": self.ok, "error": self.error,
                "attempt_count": len(self.attempts), "attempts": self.attempts, "response": self.value}


def compute_backoff(policy: RetryPolicy, attempt: int, rng: random.Random | None = None) -> float:
    """Exponential backoff with 'equal jitter', in seconds. ``attempt`` is 1-based."""
    raw = min(policy.max_delay_ms, policy.base_delay_ms * policy.multiplier ** (attempt - 1))
    jitter = (rng or random).uniform(0.5, 1.0)
    return raw * jitter / 1000.0


class DependencyGateway:
    def __init__(self, services: list[ExternalService] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 breaker_threshold: int = 5, breaker_reset_s: float = 30.0) -> None:
        services = services if services is not None else default_services()
        self._services = {s.name: s for s in services}
        self._breakers = {s.name: CircuitBreaker(breaker_threshold, breaker_reset_s) for s in services}
        self._sleep = sleep

    @property
    def service_names(self) -> set[str]:
        return set(self._services)

    def service(self, name: str) -> ExternalService:
        return self._services[name]

    def breaker(self, name: str) -> CircuitBreaker:
        return self._breakers[name]

    def call(self, name: str, params: dict[str, Any], policy: RetryPolicy) -> CallOutcome:
        service, breaker = self._services[name], self._breakers[name]
        outcome = CallOutcome(service=name, ok=False)
        for attempt in range(1, policy.max_attempts + 1):
            started = time.perf_counter()
            if not breaker.allow():
                outcome.attempts.append({"attempt": attempt, "status": "circuit_open"})
                outcome.error = f"circuit breaker open for '{name}'"
                return outcome
            try:
                value = service.call(params)
            except DependencyError as exc:
                breaker.record_failure()
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                outcome.attempts.append({"attempt": attempt, "status": "error", "error_type": type(exc).__name__,
                                         "error": str(exc), "retryable": exc.retryable, "elapsed_ms": elapsed})
                outcome.error = f"{type(exc).__name__}: {exc}"
                if not exc.retryable or attempt == policy.max_attempts:
                    return outcome
                delay = compute_backoff(policy, attempt)
                outcome.attempts[-1]["backoff_ms"] = round(delay * 1000, 2)
                self._sleep(delay)
            else:
                breaker.record_success()
                elapsed = round((time.perf_counter() - started) * 1000, 2)
                outcome.attempts.append({"attempt": attempt, "status": "ok", "elapsed_ms": elapsed})
                outcome.ok, outcome.value, outcome.error = True, value, None
                return outcome
        return outcome

    def status(self) -> dict[str, Any]:
        return {name: {"faults": svc.faults.snapshot(), "circuit": self._breakers[name].state,
                       "consecutive_failures": self._breakers[name].consecutive_failures,
                       "calls": getattr(svc, "call_count", None)}
                for name, svc in self._services.items()}


def default_services() -> list[ExternalService]:
    return [MockCreditBureau(), MockFraudCheck(), MockBusinessRegistry()]
