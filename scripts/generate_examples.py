"""Generates docs/DECISION_EXAMPLES.md by running real scenarios through the engine.

Every example in that document is produced by this script, so the documentation
can never drift from actual behaviour:   python scripts/generate_examples.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.configuration.registry import parse_config  # noqa: E402
from app.container import Settings, build_container  # noqa: E402
from app.domain.errors import DomainError  # noqa: E402

OUT = ROOT / "docs" / "DECISION_EXAMPLES.md"


def load(name: str) -> dict:
    return json.loads((ROOT / "samples" / name).read_text(encoding="utf-8"))


def block(data, lang="json") -> str:
    text = json.dumps(data, indent=2, default=str) if lang == "json" else data
    return f"```{lang}\n{text}\n```\n"


def render(c, title: str, intro: str, rid: str) -> str:
    exp = c.service.explain(rid)
    hist = c.service.history(rid)
    audit = c.service.audit(rid)
    out = [f"## {title}\n", intro + "\n",
           "**Input**\n", block(exp["input"]),
           f"**Output:** `{exp['status']}` (workflow `{exp.get('workflow')}`, "
           f"attempts: {exp.get('attempts')})\n",
           "**Reasons**\n", "\n".join(f"- {r}" for r in exp.get("reasons", [])) + "\n",
           "\n**Rules triggered / stage trace**\n", "\n".join(f"{i}. {s}" for i, s in enumerate(exp["steps"], 1)) + "\n"]
    if exp.get("data_used"):
        out += ["\n**External data used**\n", block(exp["data_used"])]
    out += ["\n**State history**\n", "| from | to | actor | reason |\n|---|---|---|---|\n" + "".join(
        f"| {h['from_status'] or '-'} | {h['to_status']} | {h['actor']} | {h['reason']} |\n" for h in hist)]
    out += ["\n**Audit events:** " + " -> ".join(f"`{e['event']}`" for e in audit["events"]) + "\n\n---\n"]
    return "\n".join(out)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        c = build_container(Settings(db_path=str(Path(tmp) / "ex.db"),
                                     config_dir=str(ROOT / "config" / "workflows")), sleep=lambda s: None)
        svc, sections = c.service, []

        r = svc.submit("loan_approval", load("loan_approve.json"), "ex-1").request
        sections.append(render(c, "1. Loan - happy path (APPROVED)",
                               "All mandatory and threshold checks pass; default branch approves.", r["request_id"]))

        r = svc.submit("loan_approval", load("loan_reject.json"), "ex-2").request
        sections.append(render(c, "2. Loan - threshold failure (REJECTED)",
                               "Credit bureau returns score 590. Every failed rule is reported, most severe first.",
                               r["request_id"]))

        r = svc.submit("loan_approval", load("loan_manual_review.json"), "ex-3").request
        svc.review(r["request_id"], "APPROVED", "underwriter.alice", "Salary slips and ITR verified")
        sections.append(render(c, "3. Loan - conditional branch (MANUAL_REVIEW -> human APPROVED)",
                               "Amount above 10 lakh routes to an underwriter; the human decision is appended to "
                               "the trace, the machine trace is preserved.", r["request_id"]))

        c.gateway.service("credit_bureau").faults.configure("fail_times", count=2)
        r = svc.submit("loan_approval", {**load("loan_approve.json"), "applicant_id": "A-010"}, "ex-4").request
        sections.append(render(c, "4. Dependency flakiness recovered by retry (APPROVED)",
                               "Credit bureau fails twice (simulated 503); gateway retries with exponential "
                               "backoff and succeeds on attempt 3. Each attempt is in the audit log.",
                               r["request_id"]))

        c.gateway.service("credit_bureau").faults.configure("fail_always")
        r = svc.submit("loan_approval", {**load("loan_approve.json"), "applicant_id": "A-011"}, "ex-5").request
        c.gateway.service("credit_bureau").faults.configure("none")
        c.gateway.breaker("credit_bureau").reset()
        svc.retry(r["request_id"])
        sections.append(render(c, "5. Dependency outage -> RETRY_PENDING -> retried -> APPROVED",
                               "Bureau is down for all 3 attempts, so the request is parked (not failed). "
                               "After recovery, POST /requests/{id}/retry re-drives it on the *pinned* workflow "
                               "version.", r["request_id"]))

        try:
            svc.submit("loan_approval", load("loan_invalid.json"))
        except DomainError as exc:
            rid = exc.details["request_id"]
            sections.append("## 6. Invalid input (HTTP 422)\n\nSchema validation reports every error at once; "
                            "the attempt is still audited as `INVALID`.\n\n**Input**\n" +
                            block(load("loan_invalid.json")) + "\n**Response**\n" +
                            block({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}) +
                            "\n**Audit events:** " + " -> ".join(
                                f"`{e['event']}`" for e in svc.audit(rid)["events"]) + "\n\n---\n")

        cfg = yaml.safe_load((ROOT / "config/workflows/loan_approval.yaml").read_text(encoding="utf-8"))
        rule = cfg["stages"]["credit_assessment"]["rules"][0]
        rule["condition"]["value"], rule["description"] = 800, "Credit score must be at least 800"
        new = c.registry.register(parse_config(cfg), auto_version=True)
        r = svc.submit("loan_approval", {**load("loan_approve.json"), "applicant_id": "A-020"}, "ex-7").request
        sections.append(render(c, f"7. Rule change via config (v1 -> v{new.version}): same applicant profile, "
                                  "new outcome",
                               "`CRD-001` threshold changed 650 -> 800 through `PUT /workflows/loan_approval` "
                               "(no code change, no restart). Example 1 remains APPROVED under v1; this new request "
                               f"is evaluated under v{new.version}.", r["request_id"]))

        r = svc.submit("claim_processing", load("claim_approve.json"), "ex-8").request
        sections.append(render(c, "8. Claim processing workflow (same engine, different config)",
                               "Coverage -> documents -> fraud check -> payout routing.", r["request_id"]))

        r = svc.submit("vendor_approval", {**load("vendor_approve.json"), "gstin": "29AABCT1332L1ZT"}, "ex-9").request
        sections.append(render(c, "9. Vendor approval workflow - compliance failure (REJECTED)",
                               "Business registry reports the GSTIN as blacklisted.", r["request_id"]))

        r1 = svc.submit("loan_approval", load("loan_approve.json"), "ex-1")
        sections.append("## 10. Duplicate request (idempotent replay)\n\nResubmitting example 1 with the same "
                        "`Idempotency-Key: ex-1` returns the stored result (HTTP 200, header "
                        "`Idempotent-Replay: true`) without re-running the workflow or calling the bureau.\n\n" +
                        block({"replayed": r1.replayed, "request_id": r1.request["request_id"],
                               "decision": r1.request["decision"], "workflow_version": r1.request["workflow_version"]}) +
                        "\nSame key with a *different* payload returns **409 idempotency_conflict**.\n")

    header = ("# Decision Explanation Examples\n\n_Generated by `python scripts/generate_examples.py` from real "
              "runs of the engine - not hand-written._\n\nEach example shows the input, rules triggered, output, "
              "and the audit reasoning (`GET /requests/{id}/explain`, `/history`, `/audit`).\n\n---\n\n")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(header + "\n".join(sections), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
