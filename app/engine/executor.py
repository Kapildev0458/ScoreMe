"""Workflow executor: walks the configured stage graph for one request.

It is deliberately *pure orchestration of config*: no persistence, no HTTP.
Given (workflow config, payload) it returns a decision, the reasons and a
complete step-by-step trace. That makes it trivially unit-testable and lets
the same engine run any workflow defined in YAML.

New stage types can be added by registering a handler in ``_handlers``.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from app.configuration.models import BranchStage, EnrichStage, RulesStage, Transition, WorkflowConfig
from app.domain.states import Decision
from app.engine.conditions import MISSING, describe_check, evaluate, resolve_path
from app.services.dependencies import DependencyGateway

_SEVERITY = {Decision.REJECTED: 2, Decision.MANUAL_REVIEW: 1}


@dataclass
class StepResult:
    step: dict[str, Any]
    transition: Transition
    reasons: list[str] = field(default_factory=list)
    dependency_call: dict[str, Any] | None = None


@dataclass
class ExecutionResult:
    decision: Decision
    reasons: list[str]
    final_stage: str
    trace: list[dict[str, Any]]
    context: dict[str, Any]
    dependency_calls: list[dict[str, Any]]


class WorkflowExecutor:
    def __init__(self, gateway: DependencyGateway) -> None:
        self._gateway = gateway
        self._handlers: dict[type, Callable[[str, Any, dict[str, Any]], StepResult]] = {
            RulesStage: self._run_rules,
            EnrichStage: self._run_enrich,
            BranchStage: self._run_branch,
        }

    def execute(self, workflow: WorkflowConfig, payload: dict[str, Any]) -> ExecutionResult:
        context = copy.deepcopy(payload)
        trace: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        stage_name = workflow.start_stage
        # Graph is validated acyclic, so this bound is purely defensive.
        for _ in range(len(workflow.stages) + 1):
            stage = workflow.stages[stage_name]
            result = self._handlers[type(stage)](stage_name, stage, context)
            result.step["transition"] = result.transition.model_dump(exclude_none=True, mode="json")
            trace.append(result.step)
            if result.dependency_call:
                calls.append(result.dependency_call)
            if result.transition.decision is not None:
                reasons = result.reasons or [result.transition.reason or "No reason configured"]
                return ExecutionResult(result.transition.decision, reasons, stage_name, trace, context, calls)
            stage_name = result.transition.next  # type: ignore[assignment]
        raise RuntimeError("workflow exceeded maximum number of steps")  # pragma: no cover

    # ------------------------------------------------------------------ stage handlers
    def _run_rules(self, name: str, stage: RulesStage, ctx: dict[str, Any]) -> StepResult:
        rule_results, failures = [], []
        for rule in stage.rules:
            passed, checks = evaluate(rule.condition, ctx)
            rule_results.append({"rule_id": rule.id, "description": rule.description, "passed": passed,
                                 "on_fail": None if passed else rule.on_fail.value, "checks": checks})
            if not passed:
                failed_checks = [describe_check(c) for c in checks if not c["passed"]] or \
                                [describe_check(c) for c in checks]
                failures.append((rule, f"[{rule.id}] {rule.reason or rule.description}: " + "; ".join(failed_checks)))

        step = {"stage": name, "type": "rules", "rules": rule_results,
                "summary": f"{len(stage.rules) - len(failures)}/{len(stage.rules)} rules passed"}
        if not failures:
            return StepResult(step, stage.on_pass)
        worst = max((r.on_fail for r, _ in failures), key=lambda d: _SEVERITY[d])
        # Report every failure, most severe first, so the explanation is complete.
        ordered = sorted(failures, key=lambda f: -_SEVERITY[f[0].on_fail])
        transition = Transition(decision=worst, reason=f"{len(failures)} rule(s) failed in stage '{name}'")
        return StepResult(step, transition, [msg for _, msg in ordered])

    def _run_enrich(self, name: str, stage: EnrichStage, ctx: dict[str, Any]) -> StepResult:
        params = {}
        for param, path in stage.inputs.items():
            value = resolve_path(ctx, path)
            params[param] = None if value is MISSING else value
        outcome = self._gateway.call(stage.service, params, stage.retry)
        step = {"stage": name, "type": "enrich", "service": stage.service, "request": params,
                "ok": outcome.ok, "attempts": outcome.attempts, "error": outcome.error}
        call = {"stage": name, **outcome.to_dict()}
        if outcome.ok:
            ctx[stage.output_key] = outcome.value
            step["response"] = outcome.value
            step["output_key"] = stage.output_key
            return StepResult(step, stage.on_success, dependency_call=call)
        reason = (stage.on_failure.reason or f"Dependency '{stage.service}' unavailable") + \
            f" (after {len(outcome.attempts)} attempt(s): {outcome.error})"
        return StepResult(step, stage.on_failure, [reason], dependency_call=call)

    def _run_branch(self, name: str, stage: BranchStage, ctx: dict[str, Any]) -> StepResult:
        evaluated = []
        for i, branch in enumerate(stage.branches):
            matched, checks = evaluate(branch.when, ctx)
            label = branch.label or f"branch[{i}]"
            evaluated.append({"branch": label, "matched": matched, "checks": checks})
            if matched:
                step = {"stage": name, "type": "branch", "branches": evaluated, "taken": label}
                reasons = []
                if branch.then.decision is not None:
                    detail = "; ".join(describe_check(c) for c in checks if c["passed"])
                    reasons = [f"{branch.then.reason or label}: {detail}"]
                return StepResult(step, branch.then, reasons)
        step = {"stage": name, "type": "branch", "branches": evaluated, "taken": "default"}
        reasons = [stage.default.reason] if stage.default.decision is not None and stage.default.reason else []
        return StepResult(step, stage.default, reasons)
