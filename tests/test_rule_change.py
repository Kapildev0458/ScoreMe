"""Rule / workflow changes via configuration (no code change, no restart)."""
import copy

import yaml

from app.domain.errors import ConfigError
from tests.conftest import CONFIG_DIR, submit


def loan_config() -> dict:
    with open(CONFIG_DIR / "loan_approval.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def set_min_score(cfg: dict, score: int) -> dict:
    cfg = copy.deepcopy(cfg)
    rule = next(r for r in cfg["stages"]["credit_assessment"]["rules"] if r["id"] == "CRD-001")
    rule["condition"]["value"] = score
    rule["description"] = f"Credit score must be at least {score}"
    return cfg


def test_threshold_change_changes_new_decisions_but_not_old(client, loan):
    old = submit(client, "loan_approval", loan, "rc-1").json()
    assert old["decision"] == "APPROVED"  # score 780 >= 650

    r = client.put("/workflows/loan_approval", json=set_min_score(loan_config(), 800))
    assert r.status_code == 201 and r.json()["active_version"] == 2

    new = submit(client, "loan_approval", {**loan, "applicant_id": "A-002"}, "rc-2").json()
    assert new["decision"] == "REJECTED" and new["workflow_version"] == 2
    assert "required >= 800" in new["reasons"][0]

    # Old decision is untouched and still attributable to v1.
    replay = submit(client, "loan_approval", loan, "rc-1").json()
    assert replay["decision"] == "APPROVED" and replay["workflow_version"] == 1
    assert client.get(f"/requests/{old['request_id']}/explain").json()["workflow"] == "loan_approval v1"


def test_add_new_rule_via_config(client, loan):
    cfg = loan_config()
    cfg["stages"]["eligibility"]["rules"].append({
        "id": "ELG-004", "description": "Tenure must be provided", "on_fail": "MANUAL_REVIEW",
        "condition": {"field": "tenure_months", "op": "exists"}})
    client.put("/workflows/loan_approval", json=cfg)
    body = submit(client, "loan_approval", loan).json()
    assert body["decision"] == "MANUAL_REVIEW" and "ELG-004" in body["reasons"][0]


def test_add_new_stage_via_config(client, loan):
    """Restructure the flow: insert an extra branch stage before eligibility."""
    cfg = loan_config()
    cfg["stages"]["vip_fast_track"] = {
        "type": "branch",
        "branches": [{"label": "vip", "when": {"field": "applicant_id", "op": "regex", "value": "^VIP-.*"},
                      "then": {"decision": "MANUAL_REVIEW", "reason": "VIP desk handles these"}}],
        "default": {"next": "eligibility"}}
    cfg["start_stage"] = "vip_fast_track"
    client.put("/workflows/loan_approval", json=cfg)
    assert submit(client, "loan_approval", {**loan, "applicant_id": "VIP-7"}).json()["decision"] == "MANUAL_REVIEW"
    assert submit(client, "loan_approval", loan).json()["decision"] == "APPROVED"


def test_invalid_config_is_rejected_and_active_version_kept(client, loan):
    cfg = loan_config()
    cfg["stages"]["eligibility"]["on_pass"] = {"next": "does_not_exist"}
    r = client.put("/workflows/loan_approval", json=cfg)
    assert r.status_code == 400 and "does_not_exist" in str(r.json())
    assert client.get("/workflows/loan_approval").json()["config"]["version"] == 1
    assert submit(client, "loan_approval", loan).json()["decision"] == "APPROVED"


def test_config_validation_catches_cycles_bad_ops_and_unknown_services(client):
    cyc = loan_config()
    cyc["stages"]["risk_routing"]["default"] = {"next": "eligibility"}
    assert "cycle" in str(client.put("/workflows/loan_approval", json=cyc).json())

    bad_op = loan_config()
    bad_op["stages"]["eligibility"]["rules"][0]["condition"]["op"] = "approximately"
    assert "unknown operator" in str(client.put("/workflows/loan_approval", json=bad_op).json())

    bad_svc = loan_config()
    bad_svc["stages"]["credit_bureau_lookup"]["service"] = "magic_api"
    assert "unknown services" in str(client.put("/workflows/loan_approval", json=bad_svc).json())

    unsafe = loan_config()
    unsafe["stages"]["credit_bureau_lookup"]["on_failure"] = {"decision": "APPROVED"}
    assert "fail-safe" in str(client.put("/workflows/loan_approval", json=unsafe).json())


def test_identical_config_does_not_create_new_version(client):
    r = client.put("/workflows/loan_approval", json=loan_config())
    assert r.json()["active_version"] == 1


def test_retry_uses_pinned_version_after_rule_change(client, loan):
    client.post("/admin/dependencies/credit_bureau/faults", json={"mode": "fail_always"})
    rid = submit(client, "loan_approval", loan).json()["request_id"]  # parked on v1
    client.put("/workflows/loan_approval", json=set_min_score(loan_config(), 800))  # v2 is stricter
    client.post("/admin/dependencies/credit_bureau/faults", json={"mode": "none"})
    retried = client.post(f"/requests/{rid}/retry").json()
    assert retried["workflow_version"] == 1 and retried["decision"] == "APPROVED"


def test_version_immutability_on_file_load(container):
    cfg = set_min_score(loan_config(), 700)  # same version number (1), different content
    from app.configuration.registry import parse_config
    try:
        container.registry.register(parse_config(cfg))
        raise AssertionError("expected ConfigError")
    except ConfigError as exc:
        assert "immutable" in exc.message


def test_versions_survive_restart(tmp_path, loan):
    from app.container import Settings, build_container
    settings = Settings(db_path=str(tmp_path / "restart.db"), config_dir=str(CONFIG_DIR))
    c1 = build_container(settings, sleep=lambda s: None)
    c1.registry.register(c1.registry.get("loan_approval").model_copy(update={"name": "Renamed"}), auto_version=True)
    c2 = build_container(settings, sleep=lambda s: None)  # "restart"
    assert c2.registry.versions("loan_approval") == [1, 2]
    assert c2.registry.get("loan_approval").name == "Renamed"
