#!/usr/bin/env python3
"""Apply and verify fixed integration-repository review and ruleset authority."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "integration-main-release-authorities.v1.json"
EVIDENCE_DIR = ROOT / "artifacts" / "integration-main-release-authorities"
TOKEN_ENV = "CODESTRA_REPOSITORY_ADMIN_TOKEN"
CONFIRMATION = "APPLY_INTEGRATION_MAIN_RELEASE_AUTHORITY_V1"
AUTHORITY_ID = "codestra.integration-main-release-authorities.v1"
RULESET_NAME = "Codestra integration protected-main release gates"
EXPECTED_OWNER = "appolon1908-hue"
EXPECTED_REVIEWER = {
    "login": "kazan555",
    "user_id": 77101516,
    "permission": "push",
    "admin": False,
}
EXPECTED_REPOSITORIES = {
    "appolon1908-hue/social.codestra.co": (
        1348783113,
        (
            "Backend policy, migration, test, and build",
            "Backend container build and hardening",
            "certify",
        ),
    ),
    "appolon1908-hue/Codestra-AI": (
        1351354401,
        ("unit-and-contract", "postgres-certification", "container-build"),
    ),
    "appolon1908-hue/Codestra-Marketing-": (
        1351352422,
        ("unit-and-contract", "postgres-certification", "container-build"),
    ),
    "appolon1908-hue/Vicidialer-Codestra": (
        1347744324,
        (
            "deploy-readiness / deploy-readiness / secret-scan",
            "deploy-readiness / deploy-readiness / source-ci",
        ),
    ),
}


class PolicyError(RuntimeError):
    """Committed policy or observed GitHub state is invalid."""


class PendingInvitation(PolicyError):
    """At least one exact reviewer invitation still needs acceptance."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot load integration authority: {path}") from exc
    require(isinstance(value, dict), "authority must be an object")
    return value


def validate_config(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    require(config.get("schema_version") == "1.0", "unsupported authority schema")
    require(config.get("authority_id") == AUTHORITY_ID, "authority ID drift")
    require(config.get("owner") == EXPECTED_OWNER, "owner drift")
    require(config.get("ruleset_name") == RULESET_NAME, "ruleset name drift")
    require(config.get("reviewer") == EXPECTED_REVIEWER, "reviewer authority drift")
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
    require(config.get("allowed_merge_methods") == ["squash"], "squash-only drift")
    require(config.get("bypass_actors") == [], "bypass actors are forbidden")

    rows = config.get("repositories")
    require(isinstance(rows, list), "repositories must be a list")
    require(len(rows) == len(EXPECTED_REPOSITORIES), "repository count drift")
    observed: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        require(isinstance(raw, dict), "repository record must be an object")
        name = raw.get("repository")
        require(isinstance(name, str) and name in EXPECTED_REPOSITORIES, "unknown repository")
        require(name not in observed, f"duplicate repository: {name}")
        observed.add(name)
        expected_id, expected_checks = EXPECTED_REPOSITORIES[name]
        require(raw.get("repository_id") == expected_id, f"{name}: stable ID drift")
        require(raw.get("default_branch") == "main", f"{name}: default branch drift")
        checks = raw.get("required_status_checks")
        require(
            isinstance(checks, list)
            and tuple(checks) == expected_checks
            and len(checks) == len(set(checks)),
            f"{name}: required status check drift",
        )
        normalized.append(dict(raw))
    require(observed == set(EXPECTED_REPOSITORIES), "repository coverage drift")
    return sorted(normalized, key=lambda row: str(row["repository"]).casefold())


def desired_ruleset(repository: Mapping[str, Any]) -> dict[str, Any]:
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
                        {"context": context}
                        for context in repository["required_status_checks"]
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
        require(isinstance(raw, Mapping), "invalid ruleset rule")
        kind = raw.get("type")
        require(isinstance(kind, str) and kind, "invalid ruleset rule type")
        require(kind not in by_type, f"duplicate ruleset rule: {kind}")
        by_type[kind] = raw
    require(
        set(by_type)
        == {
            "deletion",
            "non_fast_forward",
            "required_linear_history",
            "pull_request",
            "required_status_checks",
        },
        "ruleset rule set drift",
    )

    pull = by_type["pull_request"].get("parameters")
    status = by_type["required_status_checks"].get("parameters")
    require(isinstance(pull, Mapping), "pull request parameters missing")
    require(isinstance(status, Mapping), "status check parameters missing")
    raw_checks = status.get("required_status_checks")
    require(isinstance(raw_checks, list), "status checks missing")
    checks: list[str] = []
    for row in raw_checks:
        require(isinstance(row, Mapping), "invalid status check")
        context = row.get("context")
        require(isinstance(context, str) and context, "invalid status context")
        checks.append(context)

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
                "required_approving_review_count": pull.get(
                    "required_approving_review_count"
                ),
                "required_review_thread_resolution": pull.get(
                    "required_review_thread_resolution"
                ),
            },
            "required_status_checks": {
                "do_not_enforce_on_create": status.get("do_not_enforce_on_create"),
                "contexts": checks,
                "strict_required_status_checks_policy": status.get(
                    "strict_required_status_checks_policy"
                ),
            },
        },
    }


class GitHubApi:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base = "https://api.github.com"

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any]:
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "codestra-integration-authority-v1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                value = json.loads(raw) if raw else None
                return response.status, value
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            detail = raw.decode("utf-8", errors="replace")
            raise PolicyError(
                f"GitHub API {method} {path} failed with HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise PolicyError(f"GitHub API {method} {path} unavailable") from exc


def repo_path(name: str) -> str:
    return urllib.parse.quote(name, safe="/")


def find_ruleset(api: GitHubApi, repository: str) -> dict[str, Any] | None:
    _, payload = api.request(
        "GET",
        f"/repos/{repo_path(repository)}/rulesets?includes_parents=false&per_page=100",
    )
    require(isinstance(payload, list), f"{repository}: ruleset list invalid")
    matches = [
        row
        for row in payload
        if isinstance(row, Mapping)
        and row.get("name") == RULESET_NAME
        and row.get("source_type", "Repository") == "Repository"
    ]
    require(len(matches) <= 1, f"{repository}: duplicate named rulesets")
    return dict(matches[0]) if matches else None


def verify_ruleset(
    api: GitHubApi,
    repository: str,
    expected: Mapping[str, Any],
) -> int:
    found = find_ruleset(api, repository)
    require(found is not None, f"{repository}: integration ruleset missing")
    ruleset_id = found.get("id")
    require(isinstance(ruleset_id, int), f"{repository}: ruleset ID invalid")
    _, payload = api.request(
        "GET",
        f"/repos/{repo_path(repository)}/rulesets/{ruleset_id}",
    )
    require(isinstance(payload, Mapping), f"{repository}: ruleset readback invalid")
    require(
        normalize_ruleset(payload) == normalize_ruleset(expected),
        f"{repository}: live integration ruleset differs from committed policy",
    )
    return ruleset_id


def write_evidence(document: Mapping[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "result.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "## Integration protected-main release authorities",
        "",
        f"- Result: `{document.get('result')}`",
        f"- Mode: `{document.get('mode')}`",
        f"- Source SHA: `{document.get('source_sha')}`",
        "- Reviewer: `kazan555` (`push`, non-admin)",
        "- Required approvals: `1`",
        "- Merge method: `squash`",
        "- Bypass actors: `none`",
        "- Runtime contacted: `NO`",
        "",
        "| Repository | Reviewer | Ruleset | Result |",
        "|---|---|---:|---|",
    ]
    for row in document.get("repositories", []):
        lines.append(
            f"| `{row.get('repository')}` | `{row.get('reviewer')}` | "
            f"`{row.get('ruleset_id', '')}` | `{row.get('result')}` |"
        )
    (EVIDENCE_DIR / "result.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def execute(mode: str, confirmation: str) -> dict[str, Any]:
    config = load_config()
    repositories = validate_config(config)
    for repository in repositories:
        normalize_ruleset(desired_ruleset(repository))

    base = {
        "schema_version": "1.0",
        "mode": mode,
        "source_sha": os.environ.get("GITHUB_SHA", "local"),
        "production_changed": False,
        "runtime_contacted": False,
        "external_effects_enabled": False,
    }
    if mode == "validate":
        return {
            **base,
            "result": "PASS",
            "repositories": [
                {
                    "repository": row["repository"],
                    "reviewer": "policy-validated",
                    "ruleset_id": None,
                    "result": "PASS",
                }
                for row in repositories
            ],
        }

    require(mode in {"apply", "verify"}, "unsupported mode")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        require(
            os.environ.get("GITHUB_REPOSITORY") == "appolon1908-hue/Middleware-",
            "workflow repository drift",
        )
        require(os.environ.get("GITHUB_REF") == "refs/heads/main", "protected main required")
        require(
            os.environ.get("GITHUB_ACTOR") == os.environ.get("GITHUB_REPOSITORY_OWNER"),
            "repository owner must dispatch",
        )
    if mode == "apply":
        require(confirmation == CONFIRMATION, "exact apply confirmation required")
    token = os.environ.get(TOKEN_ENV, "")
    require(bool(token), f"{TOKEN_ENV} is required")
    api = GitHubApi(token)

    _, reviewer = api.request("GET", f"/users/{EXPECTED_REVIEWER['login']}")
    require(isinstance(reviewer, Mapping), "reviewer identity readback invalid")
    require(
        reviewer.get("id") == EXPECTED_REVIEWER["user_id"],
        "reviewer stable user ID drift",
    )

    pending = False
    results: list[dict[str, Any]] = []
    for repository in repositories:
        name = str(repository["repository"])
        encoded = repo_path(name)
        _, metadata = api.request("GET", f"/repos/{encoded}")
        require(isinstance(metadata, Mapping), f"{name}: metadata invalid")
        require(metadata.get("id") == repository["repository_id"], f"{name}: ID drift")
        require(metadata.get("default_branch") == "main", f"{name}: default branch drift")
        require(metadata.get("archived") is False, f"{name}: repository archived")
        require(metadata.get("disabled") is False, f"{name}: repository disabled")
        permissions = metadata.get("permissions")
        require(
            isinstance(permissions, Mapping) and permissions.get("admin") is True,
            f"{name}: token lacks repository administration",
        )

        reviewer_state = "verified"
        try:
            _, permission = api.request(
                "GET",
                f"/repos/{encoded}/collaborators/{EXPECTED_REVIEWER['login']}/permission",
            )
            permission_name = permission.get("permission") if isinstance(permission, Mapping) else None
            has_write = permission_name in {"admin", "maintain", "write", "push"}
        except PolicyError:
            has_write = False

        if not has_write:
            if mode == "verify":
                raise PolicyError(f"{name}: exact reviewer lacks write access")
            status, _ = api.request(
                "PUT",
                f"/repos/{encoded}/collaborators/{EXPECTED_REVIEWER['login']}",
                {"permission": "push"},
            )
            require(status in {201, 204}, f"{name}: unexpected collaborator response")
            if status == 201:
                reviewer_state = "invitation-pending"
                pending = True
            else:
                reviewer_state = "added"

        desired = desired_ruleset(repository)
        existing = find_ruleset(api, name)
        if mode == "apply":
            if existing is None:
                api.request("POST", f"/repos/{encoded}/rulesets", desired)
            else:
                ruleset_id = existing.get("id")
                require(isinstance(ruleset_id, int), f"{name}: ruleset ID invalid")
                api.request("PUT", f"/repos/{encoded}/rulesets/{ruleset_id}", desired)
        ruleset_id = verify_ruleset(api, name, desired)
        results.append(
            {
                "repository": name,
                "reviewer": reviewer_state,
                "ruleset_id": ruleset_id,
                "result": "BLOCKED" if reviewer_state == "invitation-pending" else "PASS",
            }
        )

    document = {
        **base,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "actor": os.environ.get("GITHUB_ACTOR", "local"),
        "result": "BLOCKED" if pending else "PASS",
        "repositories": results,
    }
    if pending:
        write_evidence(document)
        raise PendingInvitation(
            "exact reviewer invitations are pending; acceptance and readback are required"
        )
    return document


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
            "INTEGRATION_MAIN_RELEASE_AUTHORITY="
            f"PASS mode={args.mode} repositories={len(document['repositories'])}"
        )
        return 0
    except PendingInvitation as exc:
        print(f"INTEGRATION_MAIN_RELEASE_AUTHORITY=BLOCKED reason={exc}", file=sys.stderr)
        return 2
    except PolicyError as exc:
        document = {
            "schema_version": "1.0",
            "mode": args.mode,
            "source_sha": os.environ.get("GITHUB_SHA", "local"),
            "result": "FAIL",
            "production_changed": False,
            "runtime_contacted": False,
            "external_effects_enabled": False,
            "error": str(exc),
            "repositories": [],
        }
        write_evidence(document)
        print(f"INTEGRATION_MAIN_RELEASE_AUTHORITY=FAIL reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
