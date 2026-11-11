# ScoreMe — Configurable Workflow Decision Platform

A generic, configuration-driven decision engine. It accepts structured requests, validates them,
evaluates multi-stage rules, calls (simulated) external dependencies with retries, and produces
**explainable, auditable, idempotent** decisions: `APPROVED`, `REJECTED`, `MANUAL_REVIEW` or
`RETRY_PENDING`.

Three business workflows ship as **pure YAML configuration** on the same engine:

| Workflow | File | External dependency |
|---|---|---|
| Personal loan approval | `config/workflows/loan_approval.yaml` | `credit_bureau` |
| Insurance claim processing | `config/workflows/claim_processing.yaml` | `fraud_check` |
| Vendor onboarding approval | `config/workflows/vendor_approval.yaml` | `business_registry` |

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design, components, data flow, consistency model, trade-offs, assumptions, scaling |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Configuration model reference: how to change rules and workflows |
| [docs/DECISION_EXAMPLES.md](docs/DECISION_EXAMPLES.md) | 10 real decision explanations (input → rules → output → audit) |
| [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) | Test report: 68 tests, all passing, 95% code coverage |

---

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate        Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt

# run the tests (68 tests)
pytest

# start the API  ->  Swagger UI at http://127.0.0.1:8000/docs
uvicorn app.api.main:create_app --factory --reload
```

Environment variables (optional): `SCOREME_DB` (SQLite path, default `data/scoreme.db`),
`SCOREME_CONFIG_DIR` (default `config/workflows`), `SCOREME_ENABLE_ADMIN` (default `true`;
exposes fault-injection endpoints for demos — disable in production).

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/workflows/{workflow_id}/requests` | Submit a request (header `Idempotency-Key` recommended) |
| `GET` | `/requests/{id}` | Current status, decision, reasons |
| `GET` | `/requests/{id}/explain` | Human-readable explanation: rule trace + data used |
| `GET` | `/requests/{id}/history` | Full lifecycle (state transitions, actor, reason) |
| `GET` | `/requests/{id}/audit` | Raw audit log, all decision records, full stage trace |
| `POST` | `/requests/{id}/retry` | Re-drive a `RETRY_PENDING` / `ERROR` request |
| `POST` | `/requests/{id}/review` | Human decision for `MANUAL_REVIEW` (`{decision, reviewer, note}`) |
| `GET` | `/requests?workflow_id=&status=` | List requests (e.g. the manual-review queue) |
| `GET` | `/workflows`, `/workflows/{id}?version=` | List workflows / view a config version |
| `PUT` | `/workflows/{id}` | Publish a new config version (validated, hot, auto-versioned) |
| `POST` | `/admin/dependencies/{name}/faults` | Simulate outage: `{"mode": "fail_always" \| "fail_times" \| "timeout_always" \| "random" \| "none", "count": 2}` |
| `POST` | `/admin/storage/faults` | Simulate a failed DB commit: `{"transaction": "outcome", "times": 1}` |
| `POST` | `/admin/workflows/reload` | Re-read YAML files from disk |
| `GET` | `/health`, `/admin/dependencies` | Health, circuit-breaker and fault state |

### Status codes

`201` new decision · `200` idempotent replay (header `Idempotent-Replay: true`) · `404` unknown workflow or request ·
`409` idempotency conflict, illegal state transition or request in progress · `422` invalid input ·
`503` persistence failure (state rolled back; safe to retry with the same key).

### Demo with curl

```bash
# 1. happy path
curl -s -X POST localhost:8000/workflows/loan_approval/requests \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-1" -d @samples/loan_approve.json

# 2. duplicate -> same result, HTTP 200, no re-processing
curl -i -X POST localhost:8000/workflows/loan_approval/requests \
  -H "Content-Type: application/json" -H "Idempotency-Key: demo-1" -d @samples/loan_approve.json

# 3. explain it
curl -s localhost:8000/requests/<request_id>/explain

# 4. invalid input -> 422 with every error listed
curl -s -X POST localhost:8000/workflows/loan_approval/requests \
  -H "Content-Type: application/json" -d @samples/loan_invalid.json

# 5. dependency outage -> RETRY_PENDING, then recover and retry
curl -s -X POST localhost:8000/admin/dependencies/credit_bureau/faults -H "Content-Type: application/json" -d '{"mode":"fail_always"}'
curl -s -X POST localhost:8000/workflows/loan_approval/requests -H "Content-Type: application/json" -H "Idempotency-Key: demo-2" -d @samples/loan_approve.json
curl -s -X POST localhost:8000/admin/dependencies/credit_bureau/faults -H "Content-Type: application/json" -d '{"mode":"none"}'
curl -s -X POST localhost:8000/requests/<request_id>/retry
```

On Windows PowerShell, use `curl.exe`, or just use the Swagger UI at `/docs`.

### CLI

```bash
python -m app.cli workflows
python -m app.cli submit loan_approval samples/loan_approve.json --key cli-1
python -m app.cli explain <request_id>
python -m app.cli history <request_id>
python -m app.cli review <request_id> --decision APPROVED --reviewer alice --note "verified"
python -m app.cli retry <request_id>
```

---

## Changing rules without code

Edit thresholds, add or remove rules, add stages or re-wire branches in YAML, then either:

* **Hot (recommended):** `PUT /workflows/loan_approval` with the new config as JSON. The server validates
  it, publishes it as the next version and activates it. An invalid config is rejected, and the current
  version keeps running.
* **File:** edit the YAML, **bump `version`**, then `POST /admin/workflows/reload` or restart.

Versions are immutable. Each request is pinned to the version it arrived under, so old decisions
stay reproducible and explainable after rules change. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

A new business use case (for example employee onboarding) is a new YAML file. If it needs a new
external system, that is one small class registered in `default_services()`.

---

## Tests

```bash
pytest            # 68 tests, about 6 s
pytest -v tests/test_idempotency.py
```

| Required scenario | Test file |
|---|---|
| Happy path (all 3 workflows, every branch, manual review) | `tests/test_happy_path.py` |
| Invalid input | `tests/test_invalid_input.py` |
| Duplicate requests / idempotency (incl. 8 concurrent threads) | `tests/test_idempotency.py` |
| Dependency failure (outage, timeout, fail-safe policy, circuit breaker, backoff) | `tests/test_dependency_failure.py` |
| Retry flow (RETRY_PENDING → retry → decision, retry budget) | `tests/test_retry_flow.py` |
| Rule change (threshold change, new rule, new stage, invalid config, version pinning, restart) | `tests/test_rule_change.py` |
| Partial save failure (rollback + recovery) | `tests/test_persistence_failure.py` |
| Explainability / audit completeness | `tests/test_audit_explain.py` |
| Condition DSL unit tests | `tests/test_conditions.py` |
| CLI interface | `tests/test_cli.py` |

Full results with per-test status and coverage: [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md)
(regenerate with `python scripts/generate_test_report.py`). Regenerate the decision examples from live runs with
`python scripts/generate_examples.py`.

---

## Project layout

```
app/
  domain/          states.py (lifecycle state machine), errors.py      <- no dependencies
  engine/          conditions.py (rule DSL), input_validator.py, executor.py (stage graph runner)  <- pure, no I/O
  configuration/   models.py (typed + statically validated config), registry.py (versioning)
  services/        dependencies.py (mock externals, retry, circuit breaker), decision_service.py (use cases)
  storage/         repository.py (SQLite, transactions, idempotency constraint)
  api/             main.py (app factory, error mapping), routes.py (thin HTTP adapter)
  cli.py           command-line adapter over the same service
  container.py     composition root (dependency injection)
config/workflows/  *.yaml business workflows
samples/           example payloads
scripts/           generate_examples.py
tests/             pytest suite
docs/              ARCHITECTURE.md, CONFIGURATION.md, DECISION_EXAMPLES.md
```
