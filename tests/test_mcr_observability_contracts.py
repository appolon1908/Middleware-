import copy
import json
from pathlib import Path

from scripts.validate_mcr_observability import validate


def test_contracts_are_consistent():
    assert validate() == []


def test_validator_rejects_public_metrics_provider_effects_and_tenant_labels():
    contract = json.loads(
        Path("contracts/campaign-recycling/observability.v1.json").read_text()
    )
    for key, value in [
        ("public_metrics", True),
        ("provider_effects_enabled", True),
        ("metric_labels", ["tenant_id"]),
    ]:
        changed = copy.deepcopy(contract)
        changed[key] = value
        assert validate(contract=changed)


def test_validator_rejects_connector_activation_and_missing_evidence_contracts():
    contract = json.loads(
        Path("contracts/campaign-recycling/observability.v1.json").read_text()
    )
    changed = copy.deepcopy(contract)
    changed["connector_catalog"][0]["registration_enabled"] = True
    assert validate(contract=changed)
    for key in (
        "connector_catalog",
        "reconciliation",
        "staging_evidence",
        "rollback_observability",
        "delivery_health",
    ):
        changed = copy.deepcopy(contract)
        del changed[key]
        assert validate(contract=changed)


def test_docker_test_context_contains_only_mcr_staging_placeholder_report():
    """The contract validator must see unexecuted evidence, not private reports."""
    ignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()
    report_rule = ignore.index("reports")
    assert ignore[report_rule : report_rule + 4] == [
        "reports",
        "!reports/",
        "reports/*",
        "!reports/mcr-l-staging-evidence.json",
    ]
    assert Path("reports/mcr-l-staging-evidence.json").is_file()
    evidence = json.loads(Path("reports/mcr-l-staging-evidence.json").read_text())
    assert evidence["status"] == "not_executed"
    assert evidence["activation_blocked"] is True
    assert evidence["provider_effects_enabled"] is False
