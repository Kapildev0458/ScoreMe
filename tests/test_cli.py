"""CLI adapter: same service layer as the REST API, different transport."""
import json

import pytest

from app import cli
from tests.conftest import CONFIG_DIR, ROOT


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SCOREME_DB", str(tmp_path / "cli.db"))
    monkeypatch.setenv("SCOREME_CONFIG_DIR", str(CONFIG_DIR))

    def _run(*args):
        code = cli.main(list(args))
        return code, json.loads(capsys.readouterr().out)

    return _run


def test_cli_end_to_end(run):
    code, workflows = run("workflows")
    assert code == 0 and {w["workflow_id"] for w in workflows} == {"loan_approval", "claim_processing",
                                                                   "vendor_approval"}

    sample = str(ROOT / "samples" / "loan_manual_review.json")
    code, sub = run("submit", "loan_approval", sample, "--key", "cli-1")
    assert code == 0 and sub["status"] == "MANUAL_REVIEW" and sub["idempotent_replay"] is False
    rid = sub["request_id"]

    assert run("submit", "loan_approval", sample, "--key", "cli-1")[1]["idempotent_replay"] is True
    assert run("status", rid)[1]["status"] == "MANUAL_REVIEW"
    assert "high_value_loan" in " ".join(run("explain", rid)[1]["steps"])
    assert run("audit", rid)[1]["events"][0]["event"] == "REQUEST_RECEIVED"

    code, reviewed = run("review", rid, "--decision", "APPROVED", "--reviewer", "alice", "--note", "ok")
    assert code == 0 and reviewed["status"] == "APPROVED"
    assert [h["to_status"] for h in run("history", rid)[1]][-1] == "APPROVED"


def test_cli_reports_domain_errors_with_exit_code(run):
    code, out = run("retry", "req_does_not_exist")
    assert code == 1 and out["error"] == "request_not_found"
