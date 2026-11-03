from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from scripts import validate_security_owner_authority as authority_validator

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / "security/governance/security-owner-authority.json"
VALIDATOR = ROOT / "scripts/validate_security_owner_authority.py"


def validate(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--authority", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_historical_authority_document_is_finite_and_staging_only() -> None:
    document = json.loads(AUTHORITY.read_text())
    issued = authority_validator.parse_time(document["issued_utc"], "issued_utc")
    authority_validator.validate(document, now=issued)
    assert document["approved_scopes"] == ["server_a_isolated_staging"]
    assert set(document["prohibited_scopes"]) == {
        "production_deployment", "production_activation", "canary_activation",
        "server_b_access", "customer_data", "unrestricted_n8n_activation",
        "external_delivery",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("company", "Other LLC"),
        ("authorized_identity", "<PENDING>"),
        ("approved_scopes", ["server_a_isolated_staging", "production_deployment"]),
        ("expires_utc", "2026-01-01T00:00:00Z"),
        ("not_before_utc", "2030-01-01T00:00:00Z"),
        ("document_sha256", "0" * 64),
    ],
)
def test_authority_tampering_fails(field: str, value: object) -> None:
    document = json.loads(AUTHORITY.read_text())
    issued = authority_validator.parse_time(document["issued_utc"], "issued_utc")
    document[field] = value
    with pytest.raises(SystemExit):
        authority_validator.validate(document, now=issued)


@pytest.mark.parametrize(
    ("boundary", "offset_seconds", "active"),
    [("issued_utc", -1, False), ("issued_utc", 0, True),
     ("expires_utc", -1, True), ("expires_utc", 0, False),
     ("expires_utc", 1, False)],
)
def test_authority_validity_boundaries(
    boundary: str, offset_seconds: int, active: bool,
) -> None:
    document = json.loads(AUTHORITY.read_text())
    now = authority_validator.parse_time(document[boundary], boundary) + timedelta(seconds=offset_seconds)
    if active:
        authority_validator.validate(document, now=now)
    else:
        with pytest.raises(SystemExit, match="validity window"):
            authority_validator.validate(document, now=now)


def test_real_cli_rejects_expired_authority(tmp_path: Path) -> None:
    # A valid checksum cannot make a historical grant active at the real CLI's
    # wall clock. This fixture grants no authority outside the temporary test.
    document = json.loads(AUTHORITY.read_text())
    document.update(
        issued_utc="2000-01-01T00:00:00Z",
        not_before_utc="2000-01-01T00:00:00Z",
        expires_utc="2000-01-02T00:00:00Z",
    )
    document["document_sha256"] = hashlib.sha256(authority_validator.canonical_payload(document)).hexdigest()
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(document))
    completed = validate(path)
    assert completed.returncode != 0
    assert "validity window" in completed.stderr


def test_authority_workflow_is_protected_and_keyless() -> None:
    workflow = (ROOT / ".github/workflows/security-owner-authority-sign.yml").read_text()
    assert "environment: security-owner-authority" in workflow
    assert workflow.count("id-token: write") == 2
    assert "packages: write" not in workflow
    assert "deployments: write" not in workflow
    assert "security-owner-authority-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "cosign\" verify-blob" in workflow
    assert hashlib.sha256(AUTHORITY.read_bytes()).hexdigest() != "0" * 64
