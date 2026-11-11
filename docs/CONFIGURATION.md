# Configuration Model

A workflow is a YAML (or JSON) document. It is the **only** thing that changes between business use
cases. The engine code is identical for loans, claims and vendors.

## Top level

```yaml
workflow_id: loan_approval     # [a-z][a-z0-9_]*, used in the URL
version: 1                     # immutable once registered; bump on change (PUT auto-bumps)
name: Personal Loan Approval
description: ...
max_retry_attempts: 3          # processing attempts before RETRY_PENDING escalates to MANUAL_REVIEW
input_schema: { ... }          # intake validation
start_stage: eligibility
stages: { ... }                # a directed acyclic graph of stages
```

## Input schema

```yaml
input_schema:
  age:   { type: integer, min: 18, max: 100 }
  pan:   { type: string, pattern: "^[A-Z]{5}[0-9]{4}[A-Z]$" }
  employment_type: { type: string, enum: [salaried, self_employed, unemployed] }
  documents: { type: array, min_length: 1 }
  tenure_months: { type: integer, required: false }
```

Types: `string`, `integer`, `number`, `boolean`, `array`, `object`. Constraints: `required`
(default `true`), `min`, `max`, `enum`, `pattern`, `min_length`, `max_length`. All violations are
returned together. Booleans are never accepted as numbers.

## Stages

Every stage ends in a **transition**: either `{ next: <stage> }` or
`{ decision: APPROVED|REJECTED|MANUAL_REVIEW|RETRY_PENDING, reason: "..." }`.

### `rules`: mandatory and threshold checks

```yaml
credit_assessment:
  type: rules
  rules:
    - id: CRD-001
      description: Credit score must be at least 650
      condition: { field: credit.score, op: gte, value: 650 }
      on_fail: REJECTED            # or MANUAL_REVIEW
      reason: optional business-friendly message
  on_pass: { next: risk_routing }
```

All rules in a stage are evaluated, even after one fails, so the audit trail is complete. If any
rule fails, the most severe `on_fail` wins (`REJECTED` > `MANUAL_REVIEW`), and every failure is
listed in the reasons.

### `enrich`: external dependency call

```yaml
credit_bureau_lookup:
  type: enrich
  service: credit_bureau           # must be a registered service (checked at load time)
  inputs: { pan: pan }             # service param -> payload field path
  output_key: credit               # response becomes available as credit.* for later rules
  retry: { max_attempts: 3, base_delay_ms: 200, max_delay_ms: 2000, multiplier: 2 }
  on_success: { next: credit_assessment }
  on_failure: { decision: RETRY_PENDING, reason: Credit bureau unavailable }
```

The failure policy is a business choice and lives in config. For example, the loan workflow parks
the request (`RETRY_PENDING`), while the claim workflow sends it to `MANUAL_REVIEW`. A config that
would auto-`APPROVE` on dependency failure is rejected by the validator.

### `branch`: conditional routing

```yaml
risk_routing:
  type: branch
  branches:                        # evaluated in order, first match wins
    - label: high_value_loan
      when: { field: loan_amount, op: gt, value: 1000000 }
      then: { decision: MANUAL_REVIEW, reason: Loans above 10 lakh need underwriter sign-off }
  default: { decision: APPROVED, reason: All checks passed }
```

## Condition language

```yaml
{ field: age, op: between, value: [21, 60] }                       # constant
{ field: loan_amount, op: lte, ref: monthly_income, factor: 20 }   # compare to another field * factor
{ all: [ cond, cond ] }   { any: [ cond, cond ] }   { not: cond }  # nesting
```

Operators: `eq ne gt gte lt lte in not_in between contains not_contains regex exists not_exists
is_true is_false`. Field paths use dots (`credit.score`). A missing field makes the check fail
safely, with the note `field missing` in the trace. New operators can be added in code with
`register_operator(name, fn)`.

## Load-time validation (fail fast)

A config is rejected before it can become active if it has any of these problems:
unknown keys · `next` pointing to a missing stage · missing `start_stage` · **cycles** · unknown
operator · malformed condition (e.g. `between` without two values, invalid regex) · duplicate rule
ids · `enrich.output_key` colliding with an input field · unknown external service ·
auto-approve on dependency failure.

## Changing a workflow

| Change | How |
|---|---|
| Change a threshold | edit `value`, then `PUT /workflows/{id}` |
| Add or remove a rule | edit the stage's `rules` list |
| Add a stage or re-route | add it under `stages` and point a `next` at it |
| Change failure policy | edit `on_failure` / `retry` |
| New business use case | add a new YAML file |

`PUT /workflows/{id}` validates the config, assigns version `max+1` and activates it atomically.
Sending an identical config does not create a new version. In-flight and parked requests keep
their pinned version.
