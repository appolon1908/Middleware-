"""Fail-closed, expiring promotion exception for an independently reviewed trust fix."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "governance" / "promotion-policy.json"
GUARD = ROOT / "governance" / "promotion_guard.py"
TRUST_BRANCH = "trust/mw447-verified-validator-successor-20261008"
EXPIRY = date(2026, 10, 10)


def check(head: str, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--head", head, "--base", base],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )


def test_trust_migration_is_exact_scoped_and_expires() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    rows = [row for row in policy["migration_exceptions"]
            if row.get("head") == TRUST_BRANCH]
    assert rows == [{
        "head": TRUST_BRANCH, "base": "main", "expires": EXPIRY.isoformat(),
    }]
    assert policy["production_defaults"] == {
        "PRODUCTION_GO": "NO",
        "LIVE_CAPABILITIES_ENABLED": "NO",
        "EXTERNAL_EFFECTS": "false",
    }
    actual = check(TRUST_BRANCH, "main")
    if date.today() <= EXPIRY:
        assert actual.returncode == 0, actual.stdout + actual.stderr
        assert "PROMOTION_GUARD=PASS migration" in actual.stdout
    else:
        assert actual.returncode != 0
        assert "expired_migration" in actual.stdout


def test_trust_migration_cannot_authorize_other_targets() -> None:
    for head, base in (
        (TRUST_BRANCH + "-copy", "main"),
        ("trust/unreviewed-successor", "main"),
        (TRUST_BRANCH, "production"),
        (TRUST_BRANCH, "staging"),
    ):
        result = check(head, base)
        assert result.returncode != 0, (head, base, result.stdout)
        assert "PROMOTION_GUARD=BLOCK" in result.stdout
