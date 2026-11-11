from __future__ import annotations

import copy
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import create_app  # noqa: E402
from app.container import ROOT, Settings, build_container  # noqa: E402

CONFIG_DIR = ROOT / "config" / "workflows"

LOAN_OK = {
    "applicant_id": "A-001", "applicant_name": "Ravi Kumar", "age": 30, "pan": "ABCDE1234F",
    "monthly_income": 80000, "loan_amount": 500000, "employment_type": "salaried",
}
CLAIM_OK = {
    "claim_id": "CLM-1", "policy_id": "POL-1001", "policy_status": "active", "claim_type": "health",
    "claim_amount": 50000, "sum_insured": 500000, "days_since_incident": 10,
    "documents": ["claim_form", "id_proof", "hospital_bill"],
}
VENDOR_OK = {
    "vendor_name": "Acme Supplies", "gstin": "27AAPFU0939F1ZV", "category": "raw_materials",
    "annual_turnover": 25000000, "years_in_business": 6, "has_iso_cert": True, "contract_value": 1500000,
}


@pytest.fixture
def sleeps() -> list[float]:
    """Captures backoff delays instead of really sleeping (fast, and lets us assert on backoff)."""
    return []


@pytest.fixture
def container(tmp_path: Path, sleeps: list[float]):
    settings = Settings(db_path=str(tmp_path / "test.db"), config_dir=str(CONFIG_DIR))
    return build_container(settings, sleep=sleeps.append)


@pytest.fixture
def client(container) -> TestClient:
    return TestClient(create_app(container=container))


@pytest.fixture
def loan() -> dict:
    return copy.deepcopy(LOAN_OK)


@pytest.fixture
def claim() -> dict:
    return copy.deepcopy(CLAIM_OK)


@pytest.fixture
def vendor() -> dict:
    return copy.deepcopy(VENDOR_OK)


def submit(client: TestClient, workflow: str, payload: dict, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"/workflows/{workflow}/requests", json=payload, headers=headers)
