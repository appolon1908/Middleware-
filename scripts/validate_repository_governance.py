#!/usr/bin/env python3
"""Fail-closed validation for the repository's encoded governance baseline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "repository-governance.v1.json"
SKIP_REGISTER_PATH = ROOT / "config" / "test-skip-register.v1.json"
CODEOWNERS_PATH = ROOT / ".github" / "CODEOWNERS"
WORKFLOW_DIR = ROOT / ".github" / "workflows"
RUN_CI_PATH = ROOT / "scripts" / "run_ci.sh"
RULESET_NAME = "middleware-main-production-authority"

EXPECTED_REQUIRED_STATUS_CHECKS = frozenset(
    {
        "validate",
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
    }
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)", re.MULTILINE)
SKIP_TOKEN = re.compile(
    r"(?:pytest\.skip\s*\(|pytest\.mark\.skip(?:if)?\b|pytest\.importorskip\s*\()"
)


class GovernanceError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"{path.relative_to(ROOT)} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise GovernanceError(f"{path.relative_to(ROOT)} must contain an object")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GovernanceError(message)


def require_exact_strings(
    observed: Any,
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    require(
        isinstance(observed, list) and all(isinstance(item, str) for item in observed),
        f"{label} must be a list of strings",
    )
    observed_set = set(observed)
    missing = expected - observed_set
    unexpected = observed_set - expected
    duplicates = len(observed) != len(observed_set)
    require(
        not missing and not unexpected and not duplicates,
        (
            f"{label} drift: missing={sorted(missing)} "
            f"unexpected={sorted(unexpected)} duplicates={duplicates}"
        ),
    )


def validate_source_policy() -> dict[str, Any]:
    policy = load_json(POLICY_PATH)
    require(policy.get("schema_version") == "1.0", "unsupported governance schema")
    require(
        policy.get("repository") == "appolon1908-hue/Middleware-",
        "repository authority drift",
    )

    authority = policy.get("authority")
    require(isinstance(authority, dict), "authority policy is missing")
    require(authority.get("default_branch") == "main", "main must remain the default branch")
    for key in (
        "deployment_from_unreviewed_ref_allowed",
        "direct_push_to_default_branch_allowed",
        "force_push_allowed",
        "branch_deletion_allowed",
    ):
        require(authority.get(key) is False, f"{key} must remain false")

    merge = policy.get("merge_policy")
    require(isinstance(merge, dict), "merge policy is missing")
    expected_merge = {
        "allow_squash_merge": True,
        "allow_merge_commit": False,
        "allow_rebase_merge": False,
        "allow_auto_merge": True,
        "allow_update_branch": True,
        "delete_branch_on_merge": True,
        "web_commit_signoff_required": True,
    }
    require(merge == expected_merge, "merge policy drift")

    rules = policy.get("default_branch_ruleset")
    require(isinstance(rules, dict), "default branch ruleset is missing")
    require(rules.get("pattern") == "main", "ruleset must target main")
    require(rules.get("enforcement") == "active", "ruleset must be active")
    for key in (
        "require_pull_request",
        "require_review_thread_resolution",
        "require_linear_history",
        "require_status_checks_to_pass",
        "require_branch_up_to_date",
        "block_force_pushes",
        "block_deletions",
        "enforce_for_administrators",
    ):
        require(rules.get(key) is True, f"{key} must remain true")
    approvals = rules.get("required_approvals")
    require(
        isinstance(approvals, int) and not isinstance(approvals, bool) and approvals >= 1,
        "default branch must require at least one independent human approval",
    )
    require_exact_strings(
        rules.get("required_status_checks"),
        EXPECTED_REQUIRED_STATUS_CHECKS,
        label="required status checks",
    )

    codeowners = CODEOWNERS_PATH.read_text(encoding="utf-8")
    require(
        "@appolon1908-hue" in codeowners,
        "CODEOWNERS does not identify the repository owner",
    )

    for workflow in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        require(
            "pull_request_target:" not in text,
            f"{workflow.name}: pull_request_target is forbidden",
        )
        require("write-all" not in text, f"{workflow.name}: write-all permission is forbidden")
        for action, ref in USES.findall(text):
            if action.startswith("./"):
                continue
            require(
                FULL_SHA.fullmatch(ref) is not None,
                f"{workflow.name}: {action}@{ref} is not commit-pinned",
            )
        if "actions/checkout@" in text:
            require(
                "persist-credentials: false" in text,
                f"{workflow.name}: checkout credentials must not persist",
            )

    production_environment_users = [
        workflow.name
        for workflow in sorted(WORKFLOW_DIR.glob("*.y*ml"))
        if "environment: production" in workflow.read_text(encoding="utf-8")
    ]
    require(
        production_environment_users == ["automated-production-promotion.yml"],
        "production environment must be used only by the automated promotion gate",
    )
    promotion_gate = (WORKFLOW_DIR / production_environment_users[0]).read_text(
        encoding="utf-8"
    )
    for contract in (
        'workflows: ["Signed Middleware Release"]',
        "needs: verify-release",
        "PRODUCTION_BUSINESS_WRITES_ENABLED=NO",
        "PRODUCTION_EXTERNAL_EFFECTS_ENABLED=NONE",
    ):
        require(contract in promotion_gate, f"production promotion gate lacks {contract}")

    run_ci = RUN_CI_PATH.read_text(encoding="utf-8")
    for command in (
        "python3 scripts/validate_repository_governance.py",
        "python3 scripts/validate_automation_contract_conformance.py",
    ):
        require(command in run_ci, f"run_ci.sh does not invoke {command}")

    validate_skip_register()
    return policy


def validate_skip_register() -> None:
    register = load_json(SKIP_REGISTER_PATH)
    require(register.get("schema_version") == "1.0", "unsupported skip-register schema")
    entries = register.get("registered_files")
    require(isinstance(entries, list) and entries, "skip register is empty")
    registered: dict[str, dict[str, Any]] = {}
    for item in entries:
        require(isinstance(item, dict), "invalid skip-register entry")
        path = item.get("path")
        require(isinstance(path, str) and path, "skip-register path is invalid")
        require(path not in registered, f"duplicate skip-register entry: {path}")
        require(isinstance(item.get("gate"), str) and item["gate"], f"{path}: gate is required")
        require(
            isinstance(item.get("required_job"), str) and item["required_job"],
            f"{path}: required_job is required",
        )
        registered[path] = item

    observed: set[str] = set()
    roots = (ROOT / "tests", ROOT / "services" / "connector-runtime" / "tests")
    for test_root in roots:
        if not test_root.exists():
            continue
        for path in sorted(test_root.rglob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            if SKIP_TOKEN.search(text):
                observed.add(path.relative_to(ROOT).as_posix())

    missing = observed - set(registered)
    stale = set(registered) - observed
    require(not missing, "unregistered skipped-test files: " + ", ".join(sorted(missing)))
    require(not stale, "stale skip-register entries: " + ", ".join(sorted(stale)))

    workflow_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(WORKFLOW_DIR.glob("*.y*ml"))
    )
    for path, item in registered.items():
        require(
            item["required_job"] in workflow_text,
            f"{path}: required CI job is not present",
        )


def api_get(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codestra-middleware-governance-audit",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GovernanceError(f"GitHub API {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"GitHub API unavailable for {url}") from exc


def rules_by_type(ruleset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rules = ruleset.get("rules")
    require(isinstance(rules, list), "live ruleset rules are unavailable")
    result: dict[str, dict[str, Any]] = {}
    for item in rules:
        require(isinstance(item, dict), "live ruleset contains an invalid rule")
        rule_type = item.get("type")
        require(isinstance(rule_type, str) and rule_type, "live ruleset rule type is invalid")
        require(rule_type not in result, f"live ruleset has duplicate rule type: {rule_type}")
        result[rule_type] = item
    return result


def validate_live_ruleset(
    ruleset: dict[str, Any],
    encoded: dict[str, Any],
) -> None:
    require(ruleset.get("name") == RULESET_NAME, "live ruleset name drift")
    require(ruleset.get("target") == "branch", "live ruleset does not target branches")
    require(ruleset.get("source_type") == "Repository", "live ruleset is not repository-owned")
    require(
        ruleset.get("source") == "appolon1908-hue/Middleware-",
        "live ruleset source drift",
    )
    require(
        ruleset.get("enforcement") == encoded.get("enforcement") == "active",
        "live ruleset is not actively enforced",
    )

    require("bypass_actors" in ruleset, "admin token cannot inspect live ruleset bypass actors")
    require(ruleset.get("bypass_actors") == [], "live ruleset permits bypass actors")

    conditions = ruleset.get("conditions")
    require(isinstance(conditions, dict), "live ruleset conditions are unavailable")
    ref_name = conditions.get("ref_name")
    require(isinstance(ref_name, dict), "live ruleset ref-name condition is unavailable")
    includes = ref_name.get("include")
    excludes = ref_name.get("exclude")
    require(isinstance(includes, list), "live ruleset include condition is invalid")
    require(isinstance(excludes, list), "live ruleset exclude condition is invalid")
    allowed_targets = {"~DEFAULT_BRANCH", "refs/heads/main"}
    include_set = set(includes)
    require(
        bool(include_set) and include_set <= allowed_targets,
        "live ruleset targets refs other than main",
    )
    require(excludes == [], "live ruleset excludes protected refs")

    observed_rules = rules_by_type(ruleset)
    required_rule_types = {
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "required_status_checks",
    }
    missing_rule_types = required_rule_types - set(observed_rules)
    require(
        not missing_rule_types,
        "live ruleset missing controls: " + ", ".join(sorted(missing_rule_types)),
    )

    pull_request_parameters = observed_rules["pull_request"].get("parameters")
    require(
        isinstance(pull_request_parameters, dict),
        "live pull-request rule parameters are unavailable",
    )
    require(
        pull_request_parameters.get("required_approving_review_count")
        == encoded.get("required_approvals"),
        "live required approval count drift",
    )
    require(
        pull_request_parameters.get("dismiss_stale_reviews_on_push")
        is encoded.get("dismiss_stale_reviews"),
        "live stale-review dismissal drift",
    )
    require(
        pull_request_parameters.get("required_review_thread_resolution")
        is encoded.get("require_review_thread_resolution"),
        "live review-thread resolution drift",
    )

    status_parameters = observed_rules["required_status_checks"].get("parameters")
    require(
        isinstance(status_parameters, dict),
        "live status-check rule parameters are unavailable",
    )
    require(
        status_parameters.get("strict_required_status_checks_policy")
        is encoded.get("require_branch_up_to_date"),
        "live branch-up-to-date requirement drift",
    )
    required_checks = status_parameters.get("required_status_checks")
    require(isinstance(required_checks, list), "live required status checks are unavailable")
    contexts: list[str] = []
    for item in required_checks:
        require(isinstance(item, dict), "live required status check is invalid")
        context = item.get("context")
        require(isinstance(context, str) and context, "live status-check context is invalid")
        contexts.append(context)
    require_exact_strings(
        contexts,
        EXPECTED_REQUIRED_STATUS_CHECKS,
        label="live required status checks",
    )


def validate_live(policy: dict[str, Any]) -> None:
    token = os.environ.get("CODESTRA_REPOSITORY_ADMIN_TOKEN", "")
    require(bool(token), "CODESTRA_REPOSITORY_ADMIN_TOKEN is required for --live")
    base = "https://api.github.com/repos/appolon1908-hue/Middleware-"
    repo = api_get(base, token)
    merge = policy["merge_policy"]
    for key, expected in merge.items():
        require(repo.get(key) is expected, f"live repository setting drift: {key}")

    branch = api_get(f"{base}/branches/main", token)
    require(branch.get("protected") is True, "live main branch is not protected")

    rulesets = api_get(f"{base}/rulesets?per_page=100", token)
    require(isinstance(rulesets, list) and rulesets, "live repository has no ruleset")
    matching = [
        item
        for item in rulesets
        if isinstance(item, dict) and item.get("name") == RULESET_NAME
    ]
    require(len(matching) == 1, f"expected exactly one live ruleset named {RULESET_NAME}")
    ruleset_id = matching[0].get("id")
    require(isinstance(ruleset_id, int), "live ruleset ID is unavailable")
    detailed = api_get(
        f"{base}/rulesets/{ruleset_id}?includes_parents=false",
        token,
    )
    require(isinstance(detailed, dict), "live ruleset detail is unavailable")
    validate_live_ruleset(detailed, policy["default_branch_ruleset"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="also compare live GitHub settings")
    args = parser.parse_args()
    try:
        policy = validate_source_policy()
        if args.live:
            validate_live(policy)
    except GovernanceError as exc:
        print(f"REPOSITORY_GOVERNANCE=FAIL reason={exc}", file=sys.stderr)
        return 1
    print(
        "REPOSITORY_GOVERNANCE=PASS "
        f"live={'PASS' if args.live else 'NOT_REQUESTED'} "
        f"skip_files={len(load_json(SKIP_REGISTER_PATH)['registered_files'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
