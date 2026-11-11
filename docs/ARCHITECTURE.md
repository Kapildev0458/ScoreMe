# Architecture

## 1. Problem understanding and boundaries

The platform must turn an incoming business request into a decision. That decision must be:

1. **Correct under the current rules.** Rules are data and change frequently.
2. **Explainable.** Which rule, on which value, under which config version, produced the outcome.
3. **Safe under failure.** Flaky dependencies, crashed writes, client retries and duplicate submissions
   must never produce two decisions, a half-written decision, or an unsafe auto-approval.

**In scope:** intake and validation, rule evaluation, multi-stage workflow execution, state
lifecycle, audit, retries, idempotency, config versioning, human review hand-off.

**Out of scope (assumed handled elsewhere):** authentication and authorisation, real third-party
integrations, notifications to applicants, UI for reviewers (the API exposes the review queue and
action), and payment or disbursement.

The core idea is that **the engine knows nothing about loans, claims or vendors.** It knows about
*stages*, *rules*, *transitions* and *dependencies*. Business knowledge lives only in
`config/workflows/*.yaml`.

## 2. Component view

```
            +-------------------+      +------------------+
  HTTP ---> |  api/routes.py    |      |   cli.py         |  <- adapters (thin, no logic)
            +---------+---------+      +--------+---------+
                      |                         |
                      v                         v
            +---------------------------------------------+
            |  services/decision_service.py (use cases)   |  submit / retry / review / explain
            |  idempotency, claim (lock), atomic outcome  |
            +----+-------------+---------------+----------+
                 |             |               |
                 v             v               v
   +-------------------+ +-----------------+ +------------------------+
   | configuration/    | | engine/         | | storage/repository.py  |
   |  registry.py      | |  executor.py    | |  SQLite, transactions, |
   |  (versions, hash) | |  conditions.py  | |  UNIQUE idempotency,   |
   |  models.py        | |  input_validator| |  CAS status updates    |
   |  (static checks)  | +--------+--------+ +------------------------+
   +-------------------+          |
                                  v
                    +-----------------------------+
                    | services/dependencies.py    |
                    |  DependencyGateway          |
                    |   retry + backoff + jitter  |
                    |   circuit breaker           |
                    |  Mock services + faults     |
                    +-----------------------------+
   domain/ (states.py state machine, errors.py): used by all layers, depends on nothing
```

| Component | Responsibility | Knows about |
|---|---|---|
| `domain/states.py` | Lifecycle enum and the **only** allowed transitions | nothing |
| `domain/errors.py` | Typed domain errors with an HTTP mapping hint | nothing |
| `configuration/models.py` | Typed config schema and **static validation** (graph, cycles, operators) | condition validator |
| `configuration/registry.py` | Immutable versions, content hashes, active version, persistence of versions | repository |
| `engine/conditions.py` | Safe rule DSL (no `eval`), full per-leaf trace, operator registry | nothing |
| `engine/input_validator.py` | Intake schema validation, all errors at once | config models |
| `engine/executor.py` | Walks the stage graph and returns decision, reasons and trace. **Writes nothing.** | gateway interface |
| `services/dependencies.py` | Simulated externals, fault injection, retry policy, circuit breaker | retry policy model |
| `services/decision_service.py` | Use-case orchestration, idempotency, locking, transactional outcome, explanations | all of the above |
| `storage/repository.py` | SQL, transactions, constraints | nothing above it |
| `api/`, `cli.py` | Translate transport to service calls | service |
| `container.py` | Composition root (dependency injection) | everything (only place that does) |

**Coupling rules:** the engine is pure and storage-agnostic. Storage knows nothing about
workflows. Adapters contain no business logic. Every collaborator is constructor-injected, which is
how tests substitute a temp DB, a no-op `sleep`, injected dependency faults and injected
commit failures **without mocks or monkey-patching**.

**Extension points (open/closed):**
- New operator: `register_operator()`.
- New stage type: add a pydantic model and one handler in `WorkflowExecutor._handlers`.
- New external system: implement `call(params)` and register it.
- New use case: a YAML file.

## 3. Data flow: submitting a request

```
POST /workflows/loan_approval/requests  (Idempotency-Key: K)
 1. registry.get(active version)                      -> 404 if unknown workflow
 2. validate_input(payload, schema)                   -> 422 + persisted as INVALID (audited)
 3. TX "intake": INSERT request(status=RECEIVED, version pinned, payload_hash, key K)
                 + transition(None->RECEIVED) + audit REQUEST_RECEIVED
       UNIQUE(workflow_id, key) violated?  -> duplicate path:
           same payload hash  -> replay stored result (200, Idempotent-Replay: true)
           different payload  -> 409 idempotency_conflict
           existing in RECEIVED/ERROR -> resume processing that same request (crash recovery)
 4. TX "claim":  UPDATE status=IN_PROGRESS, attempts+1 WHERE status IN (RECEIVED,RETRY_PENDING,ERROR)
                 (compare-and-set = optimistic lock; loser gets 409 in-progress or the finished result)
 5. executor.execute(pinned config, payload)          -- pure; dependency calls are reads
       rules -> enrich (gateway: retry/backoff/breaker) -> rules -> branch -> decision
 6. RETRY_PENDING and attempts >= max_retry_attempts?  -> escalate to MANUAL_REVIEW
 7. TX "outcome": INSERT decision(trace, reasons, context, version, config_hash)
                  + CAS status IN_PROGRESS->decision + transition + audit DEPENDENCY_CALL(s) + DECISION_MADE
       commit fails -> full rollback; separate TX marks ERROR (+PROCESSING_FAILED); 503 to client
 8. 201 + {status, decision, reasons}
```

### Lifecycle (enforced by `assert_transition` and by CAS in SQL)

```
          +--> INVALID (terminal)
 (new) ---+
          +--> RECEIVED --> IN_PROGRESS --+--> APPROVED (terminal)
                               ^          +--> REJECTED (terminal)
                               |          +--> MANUAL_REVIEW --(human)--> APPROVED / REJECTED
                               |          +--> RETRY_PENDING --(retry)--+
                               |          +--> ERROR ----------(retry)--+
                               +----------------------------------------+
```

Every transition is stored with `from`, `to`, `actor` (`system` or a reviewer's name), `reason` and
a timestamp. This history is append-only.

## 4. Robustness: how each failure is handled

| Failure | Handling | Where |
|---|---|---|
| Invalid input | All errors returned (422). The attempt is persisted as `INVALID` and audited, and it does **not** consume the idempotency key, so the client can fix the input and resend with the same key. | `input_validator`, `_record_invalid` |
| Duplicate request (sequential) | DB `UNIQUE(workflow_id, idempotency_key)`. The stored result is replayed with no re-execution and no dependency call. | `submit`, `_handle_duplicate` |
| Duplicate request (concurrent) | The UNIQUE constraint lets one insert win. The CAS claim lets one worker process. Tested with 8 parallel threads: exactly one decision. | repository + `_process` |
| Same key, different payload | 409 `idempotency_conflict` (payload hash comparison, key-order independent) | `_handle_duplicate` |
| No idempotency key | Key derived from the payload hash, so accidental double-submits are still de-duplicated | `submit` |
| Transient dependency failure | Retry with exponential backoff and jitter, configured per stage. Every attempt is audited. | `DependencyGateway.call` |
| Non-retryable dependency error | Not retried (e.g. bad request) | `DependencyBadRequest.retryable=False` |
| Dependency outage | After retries, the stage's configured `on_failure` applies: `RETRY_PENDING` (park) or `MANUAL_REVIEW` (fail-safe). Never auto-approve (validator enforces this). | config + executor |
| Dependency hammering | Per-service circuit breaker (closed, open, half-open) fails fast when open | `CircuitBreaker` |
| Retry storm / poison request | Per-workflow `max_retry_attempts` budget, then escalation to `MANUAL_REVIEW` | `_process` |
| Partial save (crash mid-write) | Decision, status, transition and audit are written in **one transaction**. On failure, nothing is visible. The request moves to `ERROR` and is re-drivable via the same key or `/retry`. Tested by injecting commit failures. | `transaction()`, `_process` |
| Double processing | CAS `UPDATE ... WHERE status IN (...)` makes only one worker own a request | `compare_and_set_status` |
| Illegal state change (e.g. review an approved request) | 409 from the state machine and the CAS | `assert_transition` |
| Bad config deployed | Rejected at load or PUT with precise errors. The previous version stays active. | `WorkflowConfig` validators |
| Engine exception | Request moves to `ERROR` (recoverable), not left stuck | `_process` |

**Why re-driving is safe:** execution is deterministic given (pinned config version, payload,
dependency responses). The dependencies are reads, and the outcome write is atomic and guarded by a
CAS on `IN_PROGRESS`. Re-running can never produce two decisions for the same attempt.

## 5. Auditability and explainability

Three complementary records exist for every request:

1. **Decision records** (`decisions` table): decision, reasons, the **full stage trace** (every rule,
   every leaf check with `field`, `actual`, `op`, `expected`, and where the expected value came from,
   e.g. `monthly_income * 20`), the enriched context (external data used), `workflow_version` and the
   **config SHA-256**. One row per attempt, plus one per human decision. Nothing is overwritten.
2. **State transitions:** the lifecycle, with actor and reason.
3. **Audit log:** an append-only event stream (`REQUEST_RECEIVED`, `PROCESSING_STARTED`,
   `DEPENDENCY_CALL` with every attempt, latency and error, `DECISION_MADE`, `IDEMPOTENT_REPLAY`,
   `VALIDATION_FAILED`, `PROCESSING_FAILED`, `MANUAL_REVIEW_COMPLETED`, `WORKFLOW_VERSION_REGISTERED`).

`GET /requests/{id}/explain` turns the stored trace into sentences, for example
`CRD-001 FAIL (REJECTED): Credit score must be at least 650 [credit.score=590 (required >= 650)]`.
It reads the **stored** trace rather than recomputing it, so the explanation is what actually
happened even after the rules change. Rules in a stage are evaluated without short-circuiting, so
the explanation lists *all* reasons, not just the first.

## 6. Configurability and change tolerance

- Workflows are a typed and statically validated DAG of `rules`, `enrich` and `branch` stages (see
  [CONFIGURATION.md](CONFIGURATION.md)).
- **Versioning:** `(workflow_id, version)` is immutable and bound to a content hash. `PUT` validates,
  auto-versions and activates **without restart**. An identical config does not create a new version.
- **Pinning:** a request is pinned to the version it arrived under. Retries of parked requests
  re-run under that version, so a rule change never silently alters an in-flight case. (This is a
  deliberate choice. See trade-off T5.)
- Versions are persisted, so pinned versions survive restarts (tested).

## 7. Key design decisions and trade-offs

| # | Decision | Alternatives considered | Why this choice |
|---|---|---|---|
| T1 | **Custom, declarative condition DSL** | `eval`/Python expressions, json-logic, Drools/OPA | Safe to accept over an API (no code execution). Small enough to statically validate. Produces exactly the trace we need for explanations. Cost: less expressive than a general language. Mitigated by `ref`/`factor`, nesting and pluggable operators. |
| T2 | **Stage graph (DAG) with 3 stage types** | Flat rule list; full BPMN engine (Camunda, Temporal) | A flat list cannot express multi-step branching and dependency calls. BPMN or Temporal is heavyweight for this scope. A DAG gives branching and enrichment with simple static checks (cycles, dangling references). |
| T3 | **Evaluate all rules in a stage (no short-circuit)** | Stop at first failure | Complete explanations ("rejected for A *and* B") are worth a few extra comparisons. Short-circuiting still happens *between* stages, so expensive dependency calls are skipped when early mandatory checks fail (tested). |
| T4 | **Synchronous processing in the request** | Queue and workers, returning 202 | Simpler, and gives an immediate decision for the demo and tests. The service layer is already transport-agnostic (`_process(request_id)`), so moving it behind a queue is a deployment change, not a redesign. See §8. |
| T5 | **Pin requests to config version** | Always use the latest rules | Reproducibility and audit: a decision must be explainable by the exact rules used. Cost: a parked request may be evaluated on rules that have since been tightened. The business can still re-submit as a new request if it wants the new rules. |
| T6 | **SQLite with WAL, BEGIN IMMEDIATE, UNIQUE and CAS** | Postgres, Redis locks | Zero setup for reviewers, while still giving real ACID transactions and constraints, so the idempotency and atomicity guarantees are genuine rather than simulated. The repository is the only SQL-aware module; Postgres is a drop-in (§8). |
| T7 | **Idempotency via DB unique constraint + payload hash** | In-memory cache, Redis SETNX with TTL | Survives restarts and is race-free under concurrency without a separate store. Detects key reuse with a different payload (409). |
| T8 | **Dependency failure policy is config, with fail-safe validation** | One global behaviour | Different businesses need different behaviour (park a loan vs. hand a claim to a human). The validator forbids auto-approval on failure, a safety invariant that config cannot violate. |
| T9 | **Invalid requests persisted but not bound to the idempotency key** | Not persisted; or bound to the key | Full audit of what was attempted, while still letting a client correct the input and resend with the same key. |
| T10 | **Mock dependencies with deterministic data and a fault injector** | Random mocks, or `unittest.mock` | Reproducible tests and demos. The same fault injector powers both tests and the live admin endpoints. |
| T11 | **Retry budget escalates to MANUAL_REVIEW** | Retry forever, or reject | Bounds work per request and puts a human on edge cases rather than rejecting a customer because of *our* outage. |

## 8. Scaling considerations

The current deployment is a single process with SQLite. Here is how each concern scales:

1. **Stateless API tier.** All state lives in the DB. Run N API replicas behind a load balancer.
   Config is loaded from the DB-persisted versions, so every replica sees the same versions.
   Propagate `PUT /workflows` to other replicas via a pub/sub notification or a short poll of
   `workflow_versions`.
2. **Database.** Move to **PostgreSQL**. The repository uses only portable SQL (UNIQUE constraints,
   `UPDATE ... WHERE status IN` CAS, transactions). Partition `requests`, `decisions` and `audit_log`
   by time. Ship `audit_log` to cheaper append-only storage (S3/Parquet or Kafka) for long retention.
   Add read replicas for `/explain` and `/audit`.
3. **Asynchronous processing.** For high volume or slow dependencies, make intake write `RECEIVED`
   plus an **outbox** row in the same transaction and return **202**. Workers consume from a queue
   (SQS, Kafka, RabbitMQ) and call the existing `_process(request_id)`. The CAS claim already makes
   at-least-once delivery safe. Clients poll `GET /requests/{id}` or receive a webhook.
4. **Stuck IN_PROGRESS (worker crash).** Add `lease_expires_at` to the claim. A sweeper moves
   expired leases back to `ERROR`, which is then auto-retried. This is the one gap in the current
   single-process version; today a crash during an outage of the DB itself can leave a request
   `IN_PROGRESS`.
5. **Parked requests.** A scheduler re-drives `RETRY_PENDING` with backoff once the dependency's
   circuit closes, instead of relying on manual `/retry`.
6. **Dependencies.** Per-dependency bulkheads (connection pools or concurrency limits), timeouts,
   circuit breakers shared via Redis across replicas, and response caching where business-valid
   (a credit report valid for N hours). That also makes re-drives cheaper.
7. **Rule evaluation cost** is microseconds (in-memory comparisons). The bottleneck will be
   dependency I/O and DB writes, not the engine. Workers scale horizontally per workflow, and each
   workflow can have its own queue for isolation.
8. **Observability.** Structured logs keyed by `request_id`, metrics (decision rate by outcome,
   dependency latency and error rate, breaker state, retry-pending backlog, manual-review queue
   depth) and tracing across the stages.
9. **Multi-tenancy / governance.** Config changes would go through review (four-eyes) before
   activation, with RBAC on `PUT /workflows` and on the review endpoint. Admin and fault-injection
   endpoints would be disabled in production (`SCOREME_ENABLE_ADMIN=false`).

## 9. Assumptions

- A request belongs to exactly one workflow, and the idempotency scope is `(workflow_id, key)`.
- External dependencies are read-only lookups, so repeating them is safe. A dependency with side
  effects (e.g. disbursement) would need its own idempotency key passed downstream.
- Manual review is an external human action recorded through the API. Reviewers are trusted
  callers (no auth in scope).
- Mock dependency data is deterministic: known fixture IDs, plus a hash-based fallback for any other
  input.
- Money values are plain numbers in a single currency (INR). There is no FX or rounding policy.
- Timestamps are UTC ISO-8601.

## 10. What I would add next

1. Lease and sweeper for stuck `IN_PROGRESS` requests (§8.4) and a scheduled auto-retry of `RETRY_PENDING`.
2. A config "dry-run" endpoint that replays the last N requests against a proposed version and
   shows decision diffs before activation.
3. Per-rule decision statistics (which rules reject most) for business tuning.
4. AuthN/Z and four-eyes approval for config changes.
