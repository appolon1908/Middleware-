"""Mirror the control plane's existing, expiring PR434 migration authority."""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEAD = "convergence/middleware-saas-certified-20261005"
BASE = "fix/source-hygiene-post430-20261005"
EXPIRY = date(2026, 10, 15)


def check(head: str, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "governance/promotion_guard.py"),
         "--head", head, "--base", base],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )


def test_existing_stacked_migration_honors_its_expiry() -> None:
    result = check(HEAD, BASE)
    if date.today() <= EXPIRY:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PROMOTION_GUARD=PASS migration" in result.stdout
        assert f"expires={EXPIRY.isoformat()}" in result.stdout
    else:
        assert result.returncode != 0
        assert "expired_migration" in result.stdout


@pytest.mark.parametrize("head,base", [
    (HEAD + "-copy", BASE),
    (HEAD, BASE + "-copy"),
    (HEAD, "main"),
    (HEAD, "production"),
    (HEAD, "staging"),
])
def test_stacked_migration_cannot_authorize_other_pairs(head: str, base: str) -> None:
    result = check(head, base)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "PROMOTION_GUARD=BLOCK" in result.stdout
