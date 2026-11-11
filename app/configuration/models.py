"""Typed model of a workflow configuration file.

The whole workflow (input schema, stages, rules, branching, dependency calls,
retry policies) is data. ``WorkflowConfig`` validates it *statically* at load
time: unknown stage references, cycles, bad operators, malformed conditions
and unknown keys are all rejected before a config can ever become active.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.states import Decision
from app.engine.conditions import validate_condition


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldSpec(_Strict):
    type: Literal["string", "integer", "number", "boolean", "array", "object"]
    required: bool = True
    description: str | None = None
    min: float | None = None
    max: float | None = None
    enum: list[Any] | None = None
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None


class Transition(_Strict):
    """Where to go next: another stage (``next``) or a final ``decision``."""

    next: str | None = None
    decision: Decision | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Transition":
        if (self.next is None) == (self.decision is None):
            raise ValueError("transition must define exactly one of 'next' or 'decision'")
        return self


class Rule(_Strict):
    id: str
    description: str
    condition: dict[str, Any]
    on_fail: Decision = Decision.REJECTED
    reason: str | None = None  # business-friendly message shown when the rule fails


class RetryPolicy(_Strict):
    max_attempts: int = Field(3, ge=1, le=10)
    base_delay_ms: int = Field(100, ge=0)
    max_delay_ms: int = Field(2000, ge=0)
    multiplier: float = Field(2.0, ge=1.0)


class RulesStage(_Strict):
    """Evaluate every rule (full trace); the most severe failure wins."""

    type: Literal["rules"]
    description: str | None = None
    rules: list[Rule] = Field(min_length=1)
    on_pass: Transition


class EnrichStage(_Strict):
    """Call an external dependency and merge its response into the context."""

    type: Literal["enrich"]
    description: str | None = None
    service: str
    inputs: dict[str, str]  # service parameter -> payload field path
    output_key: str
    retry: RetryPolicy = RetryPolicy()
    on_success: Transition
    on_failure: Transition


class Branch(_Strict):
    label: str | None = None
    when: dict[str, Any]
    then: Transition


class BranchStage(_Strict):
    """Conditional routing: first matching branch wins, else ``default``."""

    type: Literal["branch"]
    description: str | None = None
    branches: list[Branch] = Field(min_length=1)
    default: Transition


Stage = Annotated[Union[RulesStage, EnrichStage, BranchStage], Field(discriminator="type")]


class WorkflowConfig(_Strict):
    workflow_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(1, ge=1)
    name: str
    description: str | None = None
    input_schema: dict[str, FieldSpec] = Field(min_length=1)
    start_stage: str
    stages: dict[str, Stage] = Field(min_length=1)
    # How many times a request may be (re)processed before RETRY_PENDING escalates to MANUAL_REVIEW.
    max_retry_attempts: int = Field(3, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_graph(self) -> "WorkflowConfig":
        errors: list[str] = []
        if self.start_stage not in self.stages:
            errors.append(f"start_stage '{self.start_stage}' is not a defined stage")

        edges: dict[str, list[str]] = {}
        for name, stage in self.stages.items():
            targets: list[tuple[str, Transition]] = []
            if isinstance(stage, RulesStage):
                targets.append(("on_pass", stage.on_pass))
                seen: set[str] = set()
                for i, rule in enumerate(stage.rules):
                    if rule.id in seen:
                        errors.append(f"stage '{name}': duplicate rule id '{rule.id}'")
                    seen.add(rule.id)
                    if rule.on_fail not in (Decision.REJECTED, Decision.MANUAL_REVIEW):
                        errors.append(f"stage '{name}' rule '{rule.id}': on_fail must be REJECTED or MANUAL_REVIEW")
                    errors.extend(validate_condition(rule.condition, f"stages.{name}.rules[{i}].condition"))
            elif isinstance(stage, EnrichStage):
                targets += [("on_success", stage.on_success), ("on_failure", stage.on_failure)]
                if stage.output_key in self.input_schema:
                    errors.append(f"stage '{name}': output_key '{stage.output_key}' collides with an input field")
                if stage.on_failure.decision == Decision.APPROVED:
                    errors.append(f"stage '{name}': on_failure must not auto-APPROVE (fail-safe rule)")
            elif isinstance(stage, BranchStage):
                targets.append(("default", stage.default))
                for i, branch in enumerate(stage.branches):
                    targets.append((f"branches[{i}]", branch.then))
                    errors.extend(validate_condition(branch.when, f"stages.{name}.branches[{i}].when"))

            edges[name] = []
            for label, t in targets:
                if t.next is not None:
                    if t.next not in self.stages:
                        errors.append(f"stage '{name}'.{label}: next stage '{t.next}' does not exist")
                    else:
                        edges[name].append(t.next)

        cycle = _find_cycle(edges)
        if cycle:
            errors.append(f"workflow contains a cycle: {' -> '.join(cycle)}")
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in edges.get(node, []):
            if color.get(nxt) == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt) == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for n in edges:
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None
