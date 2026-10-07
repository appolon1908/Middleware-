from __future__ import annotations

import copy

import pytest

from scripts.apply_repository_governance import (
    GovernanceApplyError,
    environment_payload,
    repository_patch,
    ruleset_payload,
)


@pytest.fixture()
def policy() -> dict:
    return {
        "repository_profile": {
            "description": "Codestra durable integration and automation control plane",
            "topics": [
                "middleware",
                "fastapi",
                "postgresql",
                "keycloak",
                "kong",
                "n8n",
                "integration-platform",
                "outbox",
                "idempotency",
                "gitops",
            ],
            "has_issues": True,
            "has_wiki": False,
        },
        "merge_policy": {
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "allow_auto_merge": True,
            "allow_update_branch": True,
            "delete_branch_on_merge": True,
            "web_commit_signoff_required": True,
        },
        "default_branch_ruleset": {
            "enforcement": "active",
            "required_approvals": 1,
            "dismiss_stale_reviews": True,
            "require_review_thread_resolution": True,
            "require_branch_up_to_date": True,
            "required_status_checks": [
                "Validate middleware source head",
                "Validate middleware merge result",
                "docker-runtime-build",
                "docker-test-build",
                "connector-runtime-build",
                "container-security",
                "Disposable PostgreSQL Redis integration",
                "Disposable NATS JetStream integration",
                "Temporal critical workflow integration",
                "Synthetic no-effect acceptance E2E",
            ],
        },
    }


def test_repository_patch_is_squash_only_and_secret_scanning_enabled(policy: dict) -> None:
    patch = repository_patch(policy)

    assert patch["allow_squash_merge"] is True
    assert patch["allow_merge_commit"] is False
    assert patch["allow_rebase_merge"] is False
    assert patch["allow_auto_merge"] is True
    assert patch["delete_branch_on_merge"] is True
    assert patch["web_commit_signoff_required"] is True
    assert patch["security_and_analysis"] == {
        "secret_scanning": {"status": "enabled"},
        "secret_scanning_push_protection": {"status": "enabled"},
    }


def test_ruleset_has_no_bypass_and_exact_required_checks(policy: dict) -> None:
    payload = ruleset_payload(policy)
    rules = {item["type"]: item for item in payload["rules"]}

    assert payload["enforcement"] == "active"
    assert payload["bypass_actors"] == []
    assert payload["conditions"] == {
        "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}
    }
    assert set(rules) == {
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "required_status_checks",
    }
    pull_request = rules["pull_request"]["parameters"]
    assert pull_request["allowed_merge_methods"] == ["squash"]
    assert pull_request["required_approving_review_count"] == 1
    assert (
        pull_request["require_extra_approval_for_unattributed_changes"] is False
    )
    assert pull_request["required_review_thread_resolution"] is True
    status = rules["required_status_checks"]["parameters"]
    assert status["strict_required_status_checks_policy"] is True
    assert [item["context"] for item in status["required_status_checks"]] == policy[
        "default_branch_ruleset"
    ]["required_status_checks"]


def test_ruleset_rejects_duplicate_status_checks(policy: dict) -> None:
    broken = copy.deepcopy(policy)
    broken["default_branch_ruleset"]["required_status_checks"].append(
        "Validate middleware source head"
    )

    with pytest.raises(GovernanceApplyError, match="duplicates"):
        ruleset_payload(broken)


def test_production_environment_uses_automated_gates_without_reviewers() -> None:
    payload = environment_payload(
        {
            "wait_timer": 0,
            "prevent_self_review": False,
            "reviewers": [],
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
        }
    )

    assert payload["prevent_self_review"] is False
    assert payload["reviewers"] == []
    assert payload["deployment_branch_policy"] == {
        "protected_branches": False,
        "custom_branch_policies": True,
    }


def test_environment_rejects_invalid_reviewer() -> None:
    with pytest.raises(GovernanceApplyError, match="reviewer ID"):
        environment_payload(
            {
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "id": 0}],
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
            }
        )
