from __future__ import annotations

from datetime import date
from pathlib import Path

from governance.promotion_guard import exception_reason, load_policy, promotion_allowed

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_policy(ROOT / "governance" / "promotion-policy.json")


def test_governance_main_bootstrap_is_explicit() -> None:
    assert (
        exception_reason(
            POLICY,
            "governance/agent-hierarchy-main-v1",
            "main",
            today=date(2026, 10, 7),
        )
        == "bootstrap"
    )


def test_legacy_open_pr_exception_is_bounded_and_expires() -> None:
    head = "fix/source-hygiene-post430-20261005"
    reason = exception_reason(POLICY, head, "main", today=date(2026, 10, 7))
    assert reason is not None
    assert reason.startswith("migration:pre-hierarchy-open-pr-432:")
    assert exception_reason(POLICY, head, "main", today=date(2026, 10, 16)) is None


def test_unlisted_direct_to_main_branch_remains_blocked() -> None:
    assert exception_reason(
        POLICY,
        "feature/arbitrary-direct-main",
        "main",
        today=date(2026, 10, 7),
    ) is None
    assert not promotion_allowed(POLICY, "feature/arbitrary-direct-main", "main")


def test_normal_hierarchy_remains_enforced() -> None:
    assert promotion_allowed(POLICY, "section/mw-02-command-kernel", "development")
    assert promotion_allowed(
        POLICY,
        "subsection/mw-02-command-kernel--retry",
        "section/mw-02-command-kernel",
    )
    assert not promotion_allowed(
        POLICY,
        "subsection/mw-03-connectors--retry",
        "section/mw-02-command-kernel",
    )
