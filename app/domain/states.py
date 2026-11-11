"""Request lifecycle state machine.

Every status change in the system goes through ``assert_transition`` so an
illegal jump (e.g. APPROVED -> IN_PROGRESS) is impossible regardless of which
code path tries it.
"""
from __future__ import annotations

from enum import StrEnum

from app.domain.errors import InvalidStateTransition


class Decision(StrEnum):
    """Outcome produced by a workflow run (or by a human reviewer)."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    RETRY_PENDING = "RETRY_PENDING"


class Status(StrEnum):
    """Lifecycle status of a request."""

    RECEIVED = "RECEIVED"            # persisted, not yet processed
    IN_PROGRESS = "IN_PROGRESS"      # claimed by a worker (acts as a lock)
    APPROVED = "APPROVED"            # terminal
    REJECTED = "REJECTED"            # terminal
    MANUAL_REVIEW = "MANUAL_REVIEW"  # waiting for a human
    RETRY_PENDING = "RETRY_PENDING"  # dependency unavailable; can be re-driven
    ERROR = "ERROR"                  # processing/persistence failed; can be re-driven
    INVALID = "INVALID"              # failed input validation; terminal


TERMINAL_STATUSES = frozenset({Status.APPROVED, Status.REJECTED, Status.INVALID})

# Statuses from which processing may (re)start.
PROCESSABLE_STATUSES = (Status.RECEIVED, Status.RETRY_PENDING, Status.ERROR)

ALLOWED_TRANSITIONS: dict[Status | None, frozenset[Status]] = {
    None: frozenset({Status.RECEIVED, Status.INVALID}),
    Status.RECEIVED: frozenset({Status.IN_PROGRESS}),
    Status.IN_PROGRESS: frozenset(
        {Status.APPROVED, Status.REJECTED, Status.MANUAL_REVIEW, Status.RETRY_PENDING, Status.ERROR}
    ),
    Status.RETRY_PENDING: frozenset({Status.IN_PROGRESS}),
    Status.ERROR: frozenset({Status.IN_PROGRESS}),
    Status.MANUAL_REVIEW: frozenset({Status.APPROVED, Status.REJECTED}),
    Status.APPROVED: frozenset(),
    Status.REJECTED: frozenset(),
    Status.INVALID: frozenset(),
}


def assert_transition(current: Status | None, target: Status) -> None:
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransition(
            f"Illegal state transition {current} -> {target}",
            {"from": current, "to": target, "allowed": sorted(allowed)},
        )


def status_for(decision: Decision) -> Status:
    return Status(decision.value)
