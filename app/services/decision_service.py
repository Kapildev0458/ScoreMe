"""DecisionService: the application orchestrator (use cases).

Responsibilities (and *only* these):
  intake -> validate -> idempotency -> claim -> execute workflow -> persist atomically
plus retry, manual review and explanation use cases.

Consistency model
-----------------
1. Intake transaction:   request row + RECEIVED transition + audit      (atomic)
2. Claim transaction:    CAS status -> IN_PROGRESS (+attempt counter)    (atomic, optimistic lock)
3. Workflow execution:   pure, no writes (dependency calls are reads)
4. Outcome transaction:  decision row + status + transition + audit     (atomic)

If (4) fails, nothing from the outcome is visible ("no partial save"); the
request is moved to ERROR in a separate transaction and can be safely re-driven
because step 3 is deterministic and side-effect free.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.configuration.registry import WorkflowRegistry
from app.domain.errors import (DomainError, IdempotencyConflict, InputValidationError, InvalidStateTransition,
                               PersistenceError, RequestInProgress, RequestNotFound)
from app.domain.states import PROCESSABLE_STATUSES, Decision, Status, assert_transition, status_for
from app.engine.executor import WorkflowExecutor
from app.engine.input_validator import validate_input
from app.storage.repository import Repository

log = logging.getLogger(__name__)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass
class SubmissionResult:
    request: dict[str, Any]
    replayed: bool = False


class DecisionService:
    def __init__(self, repo: Repository, registry: WorkflowRegistry, executor: WorkflowExecutor) -> None:
        self.repo = repo
        self.registry = registry
        self.executor = executor

    # ================================================================== use cases
    def submit(self, workflow_id: str, payload: Any, idempotency_key: str | None = None) -> SubmissionResult:
        workflow = self.registry.get(workflow_id)

        errors = validate_input(payload, workflow.input_schema)
        if errors:
            request_id = self._record_invalid(workflow_id, workflow.version, payload, errors)
            raise InputValidationError("Input failed schema validation",
                                       {"request_id": request_id, "errors": errors})

        digest = payload_hash(payload)
        # No client key -> derive one from the content, so accidental double-submits are still de-duplicated.
        key = idempotency_key or f"auto:{digest}"
        request_id = _new_id()
        try:
            with self.repo.transaction("intake") as conn:
                assert_transition(None, Status.RECEIVED)
                self.repo.insert_request(conn, {
                    "id": request_id, "workflow_id": workflow_id, "workflow_version": workflow.version,
                    "idempotency_key": key, "payload_hash": digest, "payload": payload, "status": Status.RECEIVED,
                })
                self.repo.add_transition(conn, request_id, None, Status.RECEIVED, "Request accepted")
                self.repo.add_audit(conn, request_id, "REQUEST_RECEIVED", {
                    "idempotency_key": key, "key_source": "client" if idempotency_key else "derived",
                    "workflow_version": workflow.version,
                    "config_hash": self.registry.hash_of(workflow_id, workflow.version),
                    "payload_hash": digest}, workflow_id=workflow_id)
        except sqlite3.IntegrityError:
            return self._handle_duplicate(workflow_id, key, digest)

        view, processed = self._process(request_id, trigger="initial")
        return SubmissionResult(view, replayed=not processed)

    def retry(self, request_id: str, actor: str = "api") -> dict[str, Any]:
        req = self._get(request_id)
        if req["status"] not in (Status.RETRY_PENDING, Status.ERROR):
            raise InvalidStateTransition(
                f"Request is {req['status']}; only RETRY_PENDING or ERROR requests can be retried",
                {"request_id": request_id, "status": req["status"]})
        return self._process(request_id, trigger="retry", actor=actor)[0]

    def review(self, request_id: str, decision: str, reviewer: str, note: str = "") -> dict[str, Any]:
        if decision not in (Decision.APPROVED, Decision.REJECTED):
            raise InputValidationError("Manual review decision must be APPROVED or REJECTED",
                                       {"errors": [{"field": "decision", "error": "must be APPROVED or REJECTED"}]})
        if not reviewer:
            raise InputValidationError("reviewer is required", {"errors": [{"field": "reviewer", "error": "is required"}]})
        req = self._get(request_id)
        target = Status(decision)
        assert_transition(Status(req["status"]), target)
        with self.repo.transaction("review") as conn:
            if not self.repo.compare_and_set_status(conn, request_id, (Status.MANUAL_REVIEW,), target, decision):
                raise InvalidStateTransition("Request is no longer awaiting manual review", {"request_id": request_id})
            reason = f"Manual review by {reviewer}: {note or 'no note'}"
            self.repo.add_decision(conn, {
                "request_id": request_id, "attempt": req["attempts"], "decision": decision, "reasons": [reason],
                "trace": [{"stage": "manual_review", "type": "human", "reviewer": reviewer, "note": note}],
                "context": {}, "final_stage": "manual_review", "workflow_version": req["workflow_version"],
                "config_hash": self.registry.hash_of(req["workflow_id"], req["workflow_version"]),
                "decided_by": f"human:{reviewer}"})
            self.repo.add_transition(conn, request_id, Status.MANUAL_REVIEW, target, reason, actor=reviewer)
            self.repo.add_audit(conn, request_id, "MANUAL_REVIEW_COMPLETED",
                                {"decision": decision, "reviewer": reviewer, "note": note},
                                workflow_id=req["workflow_id"])
        return self.get_request(request_id)

    # ================================================================== queries
    def get_request(self, request_id: str) -> dict[str, Any]:
        req = self._get(request_id)
        decisions = self.repo.list_decisions(request_id)
        latest = decisions[-1] if decisions else None
        return {
            "request_id": req["id"], "workflow_id": req["workflow_id"],
            "workflow_version": req["workflow_version"], "status": req["status"], "decision": req["decision"],
            "reasons": latest["reasons"] if latest else [], "attempts": req["attempts"],
            "idempotency_key": req["idempotency_key"], "created_at": req["created_at"],
            "updated_at": req["updated_at"],
        }

    def history(self, request_id: str) -> list[dict[str, Any]]:
        self._get(request_id)
        return self.repo.list_transitions(request_id)

    def audit(self, request_id: str) -> dict[str, Any]:
        req = self._get(request_id)
        return {"request_id": request_id, "payload": req["payload"],
                "events": self.repo.list_audit(request_id),
                "transitions": self.repo.list_transitions(request_id),
                "decisions": self.repo.list_decisions(request_id)}

    def explain(self, request_id: str) -> dict[str, Any]:
        """Human-readable explanation built from the stored trace (not recomputed)."""
        req = self._get(request_id)
        decisions = self.repo.list_decisions(request_id)
        if not decisions:
            events = self.repo.list_audit(request_id)
            return {"request_id": request_id, "status": req["status"], "decision": None,
                    "summary": f"No decision recorded yet (status {req['status']}).",
                    "steps": [f"{e['event']}: {e['details']}" for e in events], "input": req["payload"]}
        machine = [d for d in decisions if d["decided_by"] == "engine"]
        latest = decisions[-1]
        base = machine[-1] if machine else latest
        steps = [_describe_step(s) for s in base["trace"]]
        if latest is not base:
            steps.append(f"Human review: {latest['reasons'][0]}")
        return {
            "request_id": request_id,
            "workflow": f"{req['workflow_id']} v{latest['workflow_version']}",
            "config_hash": latest["config_hash"],
            "status": req["status"], "decision": latest["decision"],
            "summary": f"{latest['decision']} at stage '{latest['final_stage']}': " + " | ".join(latest["reasons"]),
            "reasons": latest["reasons"], "steps": steps, "attempts": req["attempts"],
            "input": req["payload"],
            "data_used": {k: v for k, v in base["context"].items() if k not in req["payload"]},
        }

    # ================================================================== internals
    def _process(self, request_id: str, trigger: str, actor: str = "system") -> tuple[dict[str, Any], bool]:
        """Run the workflow for a request. Returns (request view, whether *this call* processed it)."""
        req = self._get(request_id)
        workflow = self.registry.get(req["workflow_id"], req["workflow_version"])  # pinned version
        attempt = req["attempts"] + 1

        with self.repo.transaction("claim") as conn:
            claimed = self.repo.compare_and_set_status(conn, request_id, PROCESSABLE_STATUSES,
                                                       Status.IN_PROGRESS, increment_attempts=True)
            if claimed:
                self.repo.add_transition(conn, request_id, req["status"], Status.IN_PROGRESS,
                                         f"Processing started ({trigger}, attempt {attempt})", actor=actor)
                self.repo.add_audit(conn, request_id, "PROCESSING_STARTED",
                                    {"trigger": trigger, "attempt": attempt, "workflow_version": workflow.version},
                                    workflow_id=req["workflow_id"])
        if not claimed:
            current = self._get(request_id)
            if current["status"] == Status.IN_PROGRESS:
                raise RequestInProgress("Request is already being processed", {"request_id": request_id})
            return self.get_request(request_id), False  # someone else finished it concurrently

        try:
            result = self.executor.execute(workflow, req["payload"])
        except Exception as exc:  # engine bug / unexpected error -> recoverable ERROR state
            log.exception("workflow execution failed for %s", request_id)
            self._mark_error(request_id, req["workflow_id"], f"execution error: {exc}")
            raise PersistenceError("Workflow execution failed; request moved to ERROR and can be retried",
                                   {"request_id": request_id}) from exc

        decision, reasons = result.decision, list(result.reasons)
        if decision == Decision.RETRY_PENDING and attempt >= workflow.max_retry_attempts:
            decision = Decision.MANUAL_REVIEW
            reasons.append(f"Retry budget exhausted ({attempt}/{workflow.max_retry_attempts} attempts); "
                           "escalated to manual review")

        target = status_for(decision)
        assert_transition(Status.IN_PROGRESS, target)
        try:
            with self.repo.transaction("outcome") as conn:
                self.repo.add_decision(conn, {
                    "request_id": request_id, "attempt": attempt, "decision": decision, "reasons": reasons,
                    "trace": result.trace, "context": result.context, "final_stage": result.final_stage,
                    "workflow_version": workflow.version,
                    "config_hash": self.registry.hash_of(workflow.workflow_id, workflow.version),
                    "decided_by": "engine"})
                if not self.repo.compare_and_set_status(conn, request_id, (Status.IN_PROGRESS,), target, decision):
                    raise InvalidStateTransition("Request left IN_PROGRESS unexpectedly")
                self.repo.add_transition(conn, request_id, Status.IN_PROGRESS, target, "; ".join(reasons))
                for call in result.dependency_calls:
                    self.repo.add_audit(conn, request_id, "DEPENDENCY_CALL", call, workflow_id=workflow.workflow_id)
                self.repo.add_audit(conn, request_id, "DECISION_MADE", {
                    "decision": decision, "reasons": reasons, "final_stage": result.final_stage,
                    "attempt": attempt, "workflow_version": workflow.version,
                    "stages_visited": [s["stage"] for s in result.trace]}, workflow_id=workflow.workflow_id)
        except DomainError as exc:
            self._mark_error(request_id, req["workflow_id"], f"failed to persist outcome: {exc.message}")
            raise PersistenceError(
                "Decision could not be saved; outcome was rolled back. Safe to resubmit with the same "
                "Idempotency-Key or call POST /requests/{id}/retry.", {"request_id": request_id}) from exc
        except sqlite3.Error as exc:
            self._mark_error(request_id, req["workflow_id"], f"failed to persist outcome: {exc}")
            raise PersistenceError("Decision could not be saved; outcome was rolled back.",
                                   {"request_id": request_id}) from exc
        return self.get_request(request_id), True

    def _handle_duplicate(self, workflow_id: str, key: str, digest: str) -> SubmissionResult:
        existing = self.repo.get_request_by_key(workflow_id, key)
        if existing is None:  # pragma: no cover - only if the row vanished
            raise PersistenceError("Idempotency lookup failed")
        if existing["payload_hash"] != digest:
            raise IdempotencyConflict(
                "Idempotency-Key was already used with a different payload",
                {"request_id": existing["id"], "idempotency_key": key})
        if existing["status"] in (Status.RECEIVED, Status.ERROR):
            # Earlier attempt never completed (crash / failed save): resume it instead of replaying nothing.
            view, processed = self._process(existing["id"], trigger="resubmission")
            return SubmissionResult(view, replayed=not processed)
        try:
            with self.repo.transaction("replay_audit") as conn:
                self.repo.add_audit(conn, existing["id"], "IDEMPOTENT_REPLAY",
                                    {"idempotency_key": key, "status": existing["status"]}, workflow_id=workflow_id)
        except Exception:  # audit of a replay is best-effort; never fail a replay because of it
            log.warning("could not record idempotent replay for %s", existing["id"])
        return SubmissionResult(self.get_request(existing["id"]), replayed=True)

    def _record_invalid(self, workflow_id: str, version: int, payload: Any, errors: list[dict]) -> str | None:
        request_id = _new_id()
        try:
            with self.repo.transaction("invalid") as conn:
                safe_payload = payload if isinstance(payload, dict) else {"_raw": payload}
                self.repo.insert_request(conn, {
                    "id": request_id, "workflow_id": workflow_id, "workflow_version": version,
                    "idempotency_key": None,  # invalid requests do not consume an idempotency key
                    "payload_hash": payload_hash(safe_payload), "payload": safe_payload, "status": Status.INVALID})
                self.repo.add_transition(conn, request_id, None, Status.INVALID, "Input failed schema validation")
                self.repo.add_audit(conn, request_id, "VALIDATION_FAILED", {"errors": errors}, workflow_id=workflow_id)
            return request_id
        except Exception:
            log.exception("could not record invalid request")
            return None

    def _mark_error(self, request_id: str, workflow_id: str, reason: str) -> None:
        try:
            with self.repo.transaction("error") as conn:
                if self.repo.compare_and_set_status(conn, request_id, (Status.IN_PROGRESS,), Status.ERROR):
                    self.repo.add_transition(conn, request_id, Status.IN_PROGRESS, Status.ERROR, reason)
                    self.repo.add_audit(conn, request_id, "PROCESSING_FAILED", {"reason": reason},
                                        workflow_id=workflow_id)
        except Exception:
            # Storage is fully down. The request stays IN_PROGRESS; see ARCHITECTURE.md (stale-lease recovery).
            log.exception("could not mark request %s as ERROR", request_id)

    def _get(self, request_id: str) -> dict[str, Any]:
        req = self.repo.get_request(request_id)
        if req is None:
            raise RequestNotFound(f"Request '{request_id}' not found")
        return req


def _new_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def _describe_step(step: dict[str, Any]) -> str:
    from app.engine.conditions import describe_check

    where = step.get("transition", {})
    goto = f"-> {where['decision']}" if "decision" in where else f"-> next stage '{where.get('next')}'"
    if step["type"] == "rules":
        parts = []
        for r in step["rules"]:
            mark = "PASS" if r["passed"] else f"FAIL ({r['on_fail']})"
            checks = "; ".join(describe_check(c) for c in r["checks"])
            parts.append(f"{r['rule_id']} {mark}: {r['description']} [{checks}]")
        return f"Stage '{step['stage']}' (rules, {step['summary']}) {goto}. " + " | ".join(parts)
    if step["type"] == "enrich":
        n = len(step["attempts"])
        if step["ok"]:
            return (f"Stage '{step['stage']}' called '{step['service']}' -> success after {n} attempt(s); "
                    f"response stored as '{step['output_key']}': {step['response']} {goto}")
        return f"Stage '{step['stage']}' called '{step['service']}' -> FAILED after {n} attempt(s) ({step['error']}) {goto}"
    if step["type"] == "branch":
        taken = step["taken"]
        return f"Stage '{step['stage']}' (branch) took '{taken}' {goto}"
    return f"Stage '{step.get('stage')}' {goto}"
