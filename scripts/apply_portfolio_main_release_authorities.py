#!/usr/bin/env python3
"""Validate, apply, and verify exact default-branch release rulesets."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from portfolio_ruleset.github_api import GitHubApi
from portfolio_ruleset.common import RolloutError

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "portfolio-main-release-authorities.v1.json"
EVIDENCE_DIR = ROOT / "artifacts" / "portfolio-main-release-authorities"
TOKEN_ENV = "CODESTRA_REPOSITORY_ADMIN_TOKEN"
CONFIRMATION = "APPLY_MAIN_RELEASE_AUTHORITY_V1"
RULESET_NAME = "Codestra protected default-branch release gates"
EXPECTED_REPOSITORIES = {
    "appolon1908-hue/codestra": (1319808791, ("verify", "container")),
    "appolon1908-hue/backend2": (1319903950, ("validate", "container")),
    "appolon1908-hue/Telnexa-web": (
        1346958528,
        ("validate-build-smoke", "docker-build"),
    ),
    "appolon1908-hue/scrapper": (
        1329513537,
        ("deployment-policy", "validate"),
    ),
    "appolon1908-hue/Breero.com": (1331354808, ("quality",)),
    "appolon1908-hue/Moneybee-Backend": (
        1343760409,
        (
            "verify",
            "postgres-identity-tenancy",
            "containers (api)",
            "containers (worker)",
            "containers (migrate)",
            "application",
            "deployment-policy",
        ),
    ),
}


class PolicyError(RuntimeError):
    """The committed policy or observed GitHub state is not acceptable."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot load ruleset authority: {path}") from exc
    require(isinstance(value, dict), "authority must be an object")
    return value


def validate_config(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    require(config.get("schema_version") == "1.0", "unsupported authority schema")
    require(
        config.get("authority_id") == "codestra.portfolio-main-release-authorities.v1",
        "authority ID drift",
    )
    require(config.get("owner") == "appolon1908-hue", "owner drift")
    require(config.get("ruleset_name") == RULESET_NAME, "ruleset name drift")
    require(config.get("required_approvals") == 1, "exactly one approval is required")
    require(config.get("dismiss_stale_reviews") is True, "stale reviews must be dismissed")
    require(
        config.get("require_last_push_approval") is True,
        "last-push approval must be required",
    )
    require(
        config.get("require_review_thread_resolution") is True,
        "review threads must be resolved",
    )
    require(
        config.get("require_branch_up_to_date") is True,
        "strict current-base checks are required",
    )
    require(config.get("allowed_merge_methods") == ["squash"], "squash-only policy drift")

    repositories = config.get("repositories")
    require(isinstance(repositories, list), "repositories must be a list")
    require(len(repositories) == len(EXPECTED_REPOSITORIES), "repository count drift")
    observed_names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in repositories:
        require(isinstance(raw, dict), "repository record must be an object")
        name = raw.get("repository")
        require(isinstance(name, str) and name in EXPECTED_REPOSITORIES, "unknown repository")
        require(name not in observed_names, f"duplicate repository: {name}")
        observed_names.add(name)
        expected_id, expected_checks = EXPECTED_REPOSITORIES[name]
        require(raw.get("repository_id") == expected_id, f"{name}: stable ID drift")
        require(raw.get("default_branch") == "main", f"{name}: default branch drift")
        checks = raw.get("required_status_checks")
        require(
            isinstance(checks, list)
            and tuple(checks) == expected_checks
            and len(checks) == len(set(checks)),
            f"{name}: required check policy drift",
        )
        normalized.append(dict(raw))
    require(observed_names == set(EXPECTED_REPOSITORIES), "repository coverage drift")
    return sorted(normalized, key=lambda item: str(item["repository"]).casefold())


def desired_ruleset(config: Mapping[str, Any], repository: Mapping[str, Any]) -> dict[str, Any]:
    checks = repository["required_status_checks"]
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["~DEFAULT_BRANCH"],
                "exclude": [],
            }
        },
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["squash"],
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": True,
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {"context": context} for context in checks
                    ],
                    "strict_required_status_checks_policy": True,
                },
            },
        ],
    }


def normalize_ruleset(value: Mapping[str, Any]) -> dict[str, Any]:
    conditions = value.get("conditions")
    require(isinstance(conditions, Mapping), "ruleset conditions missing")
    ref_name = conditions.get("ref_name")
    require(isinstance(ref_name, Mapping), "ruleset ref conditions missing")
    rules = value.get("rules")
    require(isinstance(rules, list), "ruleset rules missing")
    by_type: dict[str, Mapping[str, Any]] = {}
    for raw in rules:
        require(isinstance(raw, Mapping), "ruleset contains an invalid rule")
        rule_type = raw.get("type")
        require(isinstance(rule_type, str) and rule_type, "ruleset rule type invalid")
        require(rule_type not in by_type, f"duplicate ruleset rule: {rule_type}")
        by_type[rule_type] = raw
    required_types = {
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "required_status_checks",
    }
    require(set(by_type) == required_types, "ruleset rule set drift")

    pull = by_type["pull_request"].get("parameters")
    require(isinstance(pull, Mapping), "pull-request parameters missing")
    status = by_type["required_status_checks"].get("parameters")
    require(isinstance(status, Mapping), "status-check parameters missing")
    status_rows = status.get("required_status_checks")
    require(isinstance(status_rows, list), "required status checks missing")
    contexts: list[str] = []
    for row in status_rows:
        require(isinstance(row, Mapping), "invalid status-check record")
        context = row.get("context")
        require(isinstance(context, str) and context, "invalid status-check context")
        contexts.append(context)
    return {
        "name": value.get("name"),
        "target": value.get("target"),
        "enforcement": value.get("enforcement"),
        "bypass_actors": value.get("bypass_actors"),
        "conditions": {
            "ref_name": {
                "include": list(ref_name.get("include", [])),
                "exclude": list(ref_name.get("exclude", [])),
            }
        },
        "rules": {
            "deletion": True,
            "non_fast_forward": True,
            "required_linear_history": True,
            "pull_request": {
                "allowed_merge_methods": list(pull.get("allowed_merge_methods", [])),
                "dismiss_stale_reviews_on_push": pull.get("dismiss_stale_reviews_on_push"),
                "require_code_owner_review": pull.get("require_code_owner_review"),
                "require_last_push_approval": pull.get("require_last_push_approval"),
                "required_approving_review_count": pull.get("required_approving_review_count"),
                "required_review_thread_resolution": pull.get("required_review_thread_resolution"),
            },
            "required_status_checks": {
                "do_not_enforce_on_create": status.get("do_not_enforce_on_create"),
                "contexts": contexts,
                "strict_required_status_checks_policy": status.get(
                    "strict_required_status_checks_policy"
                ),
            },
        },
    }


def repo_path(name: str) -> str:
    return urllib.parse.quote(name, safe="/")


def find_ruleset(api: GitHubApi, repository: str) -> dict[str, Any] | None:
    matches = [
        row
        for row in api.list_rulesets(repository)
        if row.get("name") == RULESET_NAME
        and row.get("source_type", "Repository") == "Repository"
    ]
    require(len(matches) <= 1, f"{repository}: duplicate named rulesets")
    return matches[0] if matches else None


def verify_live(
    api: GitHubApi,
    config: Mapping[str, Any],
    repository: Mapping[str, Any],
) -> int:
    full_name = str(repository["repository"])
    existing = find_ruleset(api, full_name)
    require(existing is not None, f"{full_name}: ruleset missing")
    ruleset_id = existing.get("id")
    require(isinstance(ruleset_id, int), f"{full_name}: ruleset ID invalid")
    observed = normalize_ruleset(api.get_ruleset(full_name, ruleset_id))
    expected = normalize_ruleset(desired_ruleset(config, repository))
    require(observed == expected, f"{full_name}: live ruleset differs from policy")
    return ruleset_id


def write_evidence(document: Mapping[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "result.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "## Protected default-branch release authorities",
        "",
        f"- Result: `{document.get('result')}`",
        f"- Mode: `{document.get('mode')}`",
        f"- Source SHA: `{document.get('source_sha')}`",
        "- Target: `~DEFAULT_BRANCH`",
        "- Approvals: `1`",
        "- Merge method: `squash`",
        "- Bypass actors: `none`",
        "",
        "| Repository | Action | Ruleset | Result |",
        "|---|---|---:|---|",
    ]
    for row in document.get("repositories", []):
        lines.append(
            f"| `{row.get('repository')}` | `{row.get('action')}` | "
            f"`{row.get('ruleset_id', '')}` | `{row.get('result')}` |"
        )
    (EVIDENCE_DIR / "result.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def execute(mode: str, confirmation: str) -> dict[str, Any]:
    config = load_config()
    repositories = validate_config(config)
    if mode == "validate":
        for repository in repositories:
            normalize_ruleset(desired_ruleset(config, repository))
        return {
            "schema_version": "1.0",
            "mode": mode,
            "source_sha": os.environ.get("GITHUB_SHA", "local"),
            "result": "PASS",
            "repositories": [
                {
                    "repository": item["repository"],
                    "action": "policy-validated",
                    "result": "PASS",
                }
                for item in repositories
            ],
        }

    require(mode in {"apply", "verify"}, "unsupported mode")
    if mode == "apply":
        require(confirmation == CONFIRMATION, "exact apply confirmation required")
    token = os.environ.get(TOKEN_ENV, "")
    require(bool(token), f"{TOKEN_ENV} is required")
    api = GitHubApi(token)

    preflight: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for repository in repositories:
        full_name = str(repository["repository"])
        metadata = api.request("GET", f"/repos/{repo_path(full_name)}").payload
        require(isinstance(metadata, dict), f"{full_name}: repository metadata invalid")
        require(metadata.get("id") == repository["repository_id"], f"{full_name}: ID drift")
        require(metadata.get("default_branch") == "main", f"{full_name}: default branch drift")
        require(metadata.get("archived") is False, f"{full_name}: repository is archived")
        require(metadata.get("disabled") is False, f"{full_name}: repository is disabled")
        permissions = metadata.get("permissions")
        require(
            isinstance(permissions, Mapping) and permissions.get("admin") is True,
            f"{full_name}: token lacks repository administration",
        )
        existing = find_ruleset(api, full_name)
        preflight.append((repository, existing))

    results: list[dict[str, Any]] = []
    for repository, existing in preflight:
        full_name = str(repository["repository"])
        desired = desired_ruleset(config, repository)
        action = "verify"
        if mode == "apply":
            _, action = api.upsert_ruleset(full_name, desired, existing)
        ruleset_id = verify_live(api, config, repository)
        results.append(
            {
                "repository": full_name,
                "action": action,
                "ruleset_id": ruleset_id,
                "result": "PASS",
            }
        )
    return {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": mode,
        "source_sha": os.environ.get("GITHUB_SHA", "local"),
        "actor": os.environ.get("GITHUB_ACTOR", "local"),
        "result": "PASS",
        "production_changed": False,
        "runtime_contacted": False,
        "repositories": results,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("validate", "apply", "verify"), default="validate")
    parser.add_argument("--confirm", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        document = execute(args.mode, args.confirm)
        write_evidence(document)
        print(
            "PORTFOLIO_MAIN_RELEASE_AUTHORITY="
            f"PASS mode={args.mode} repositories={len(document['repositories'])}"
        )
        return 0
    except (PolicyError, RolloutError) as exc:
        document = {
            "schema_version": "1.0",
            "mode": args.mode,
            "source_sha": os.environ.get("GITHUB_SHA", "local"),
            "result": "FAIL",
            "production_changed": False,
            "runtime_contacted": False,
            "error": str(exc),
            "repositories": [],
        }
        write_evidence(document)
        print(f"PORTFOLIO_MAIN_RELEASE_AUTHORITY=FAIL reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
