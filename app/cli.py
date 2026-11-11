"""Command-line interface (uses the same service layer as the REST API).

Examples:
  python -m app.cli workflows
  python -m app.cli submit loan_approval samples/loan_approve.json --key demo-1
  python -m app.cli explain <request_id>
  python -m app.cli history <request_id>
  python -m app.cli retry <request_id>
  python -m app.cli review <request_id> --decision APPROVED --reviewer alice --note "verified salary slips"
  python -m app.cli fault credit_bureau fail_always
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.container import build_container
from app.domain.errors import DomainError


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scoreme", description="Workflow decision platform CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("workflows", help="list workflows and versions")
    p = sub.add_parser("submit", help="submit a request from a JSON file")
    p.add_argument("workflow_id")
    p.add_argument("payload_file")
    p.add_argument("--key", help="idempotency key")
    for name in ("status", "explain", "history", "audit", "retry"):
        sub.add_parser(name).add_argument("request_id")
    r = sub.add_parser("review", help="complete a manual review")
    r.add_argument("request_id")
    r.add_argument("--decision", required=True, choices=["APPROVED", "REJECTED"])
    r.add_argument("--reviewer", required=True)
    r.add_argument("--note", default="")
    args = parser.parse_args(argv)

    c = build_container()
    svc = c.service
    try:
        if args.cmd == "workflows":
            _print(c.registry.list())
        elif args.cmd == "submit":
            with open(args.payload_file, encoding="utf-8") as fh:
                payload = json.load(fh)
            result = svc.submit(args.workflow_id, payload, args.key)
            _print({**result.request, "idempotent_replay": result.replayed})
        elif args.cmd == "status":
            _print(svc.get_request(args.request_id))
        elif args.cmd == "explain":
            _print(svc.explain(args.request_id))
        elif args.cmd == "history":
            _print(svc.history(args.request_id))
        elif args.cmd == "audit":
            _print(svc.audit(args.request_id))
        elif args.cmd == "retry":
            _print(svc.retry(args.request_id))
        elif args.cmd == "review":
            _print(svc.review(args.request_id, args.decision, args.reviewer, args.note))
    except DomainError as exc:
        _print({"error": exc.code, "message": exc.message, "details": exc.details})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
