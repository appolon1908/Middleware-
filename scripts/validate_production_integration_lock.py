#!/usr/bin/env python3
"""Fail-closed validation for the full Codestra production integration lock."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "config" / "production-integration-lock.v1.json"
EVIDENCE_DIR = ROOT / "artifacts" / "production-integration-lock"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO_RE = re.compile(r"^appolon1908-hue/[A-Za-z0-9_.-]+$")
SOURCE_STATES = {
    "protected_source_ready",
    "candidate_pending_review",
    "candidate_needs_refresh",
    "governance_incomplete",
    "source_only",
    "authority_only",
}
CHECK_STATES = {"active", "missing", "unverified"}
REVIEWER_STATES = {"available", "requested", "pending-access", "unverified"}
BINDING_STATES = {
    "source-ready",
    "source-only",
    "runtime-unverified",
    "not-observed",
    "unverified",
}
PRODUCTION_STATES = {"BLOCKED", "SOURCE_READY", "PENDING_PROTECTED_MERGE"}
CANDIDATE_STATES = {
    "refresh-from-main-required",
    "pending-review-before-development-reconciliation",
    "superseded-by-reconciled-development-head",
    "pending-exact-head-review",
    "blocked-by-pr-55-and-refresh",
    "pending-review-and-source-pin-refresh",
    "ci-running-and-reviewer-access-pending",
    "reviewer-access-pending",
    "independent-review-requested",
    "reviewer-and-ruleset-access-pending",
}
GLOBAL_GATES = [
    "protected_merged_source",
    "exact_head_ci",
    "independent_approval",
    "signed_immutable_artifact",
    "staging_readonly_deployment",
    "source_and_digest_readback",
    "identity_and_route_matrix",
    "provider_readback",
    "backup_restore",
    "rollback_rehearsal",
    "monitoring_continuity",
    "bounded_read_only_canary",
]
COMPONENT_DEFAULT_KEYS = {
    "branch",
    "source",
    "protected",
    "checks",
    "reviewer",
    "prs",
    "deps",
    "binding",
    "state",
    "blockers",
}
COMPONENT_REQUIRED_KEYS = {"id", "repo", "rid", "sha"}
COMPONENT_ALLOWED_KEYS = COMPONENT_REQUIRED_KEYS | COMPONENT_DEFAULT_KEYS
CERTIFICATION_FIELDS = {
    "immutable_image_digest",
    "staging_evidence",
    "backup_restore_evidence",
    "rollback_evidence",
    "runtime_readback_evidence",
}


class LockError(RuntimeError):
    """The integration lock would permit an unsupported production claim."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LockError(message)


def load_lock(path: Path = LOCK_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot load integration lock: {path}") from exc
    require(isinstance(value, dict), "integration lock must be an object")
    return value


def expand_component(raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    require(set(raw).issubset(COMPONENT_ALLOWED_KEYS), "unknown component field")
    require(COMPONENT_REQUIRED_KEYS.issubset(raw), "component identity fields missing")
    row = copy.deepcopy(dict(defaults))
    row.update(copy.deepcopy(dict(raw)))
    return row


def validate_candidate(component_id: str, candidate: Mapping[str, Any]) -> None:
    require(
        set(candidate) == {"n", "sha", "base", "status", "merge"},
        f"{component_id}: candidate field drift",
    )
    require(
        isinstance(candidate.get("n"), int) and candidate["n"] > 0,
        f"{component_id}: invalid PR number",
    )
    require(
        isinstance(candidate.get("sha"), str)
        and SHA_RE.fullmatch(candidate["sha"]),
        f"{component_id}: invalid candidate SHA",
    )
    require(
        isinstance(candidate.get("base"), str) and candidate["base"],
        f"{component_id}: candidate base missing",
    )
    require(
        candidate.get("status") in CANDIDATE_STATES,
        f"{component_id}: invalid candidate status",
    )
    require(
        candidate.get("merge") in {"squash", "merge"},
        f"{component_id}: invalid merge method",
    )


def validate_certification(
    component: Mapping[str, Any],
    certification: Mapping[str, Any],
) -> None:
    cid = str(component["id"])
    require(set(certification) == CERTIFICATION_FIELDS, f"{cid}: certification field drift")
    digest = certification.get("immutable_image_digest")
    require(
        isinstance(digest, str) and DIGEST_RE.fullmatch(digest),
        f"{cid}: immutable digest required",
    )
    for field in CERTIFICATION_FIELDS - {"immutable_image_digest"}:
        value = certification.get(field)
        require(isinstance(value, str) and value.strip(), f"{cid}: {field} required")
    require(
        component["source"] == "protected_source_ready",
        f"{cid}: certified runtime requires protected source",
    )
    require(component["protected"] is True, f"{cid}: protected branch required")
    require(component["checks"] == "active", f"{cid}: active required checks required")


def validate_lock(value: Mapping[str, Any]) -> dict[str, Any]:
    require(value.get("schema_version") == "1.0", "unsupported lock schema")
    require(
        value.get("lock_id") == "codestra.middleware-full-production-integration.v1",
        "lock ID drift",
    )
    require(value.get("decision") == "NO_GO", "source lock decision must remain NO_GO")
    require(value.get("production_activated") is False, "production must remain inactive")

    authority = value.get("authority")
    require(isinstance(authority, Mapping), "authority missing")
    require(
        authority.get("repository") == "appolon1908-hue/Middleware-",
        "authority repository drift",
    )
    base_sha = authority.get("base_sha")
    require(
        isinstance(base_sha, str) and SHA_RE.fullmatch(base_sha),
        "authority base SHA invalid",
    )

    policy = value.get("release_policy")
    require(isinstance(policy, Mapping), "release policy missing")
    require(policy.get("immutable_artifacts_only") is True, "immutable artifacts are required")
    require(policy.get("rebuild_between_environments") is False, "environment rebuilds are forbidden")
    percent = policy.get("max_read_only_canary_percent")
    require(
        isinstance(percent, int) and 0 < percent <= 1,
        "read-only canary must be <=1 percent",
    )
    require(
        policy.get("read_only_canary_methods") == ["GET", "HEAD"],
        "read-only canary methods drift",
    )
    require(
        policy.get("required_global_gates") == GLOBAL_GATES,
        "global gate order or coverage drift",
    )
    require(policy.get("calls_placed") == 0, "CALLS_PLACED must remain zero")
    effects = policy.get("external_effects")
    require(isinstance(effects, Mapping) and effects, "external effects registry missing")
    for name, enabled in effects.items():
        require(isinstance(name, str) and name, "invalid external effect name")
        require(enabled is False, f"external effect must remain disabled: {name}")

    defaults = value.get("component_defaults")
    require(isinstance(defaults, Mapping), "component defaults missing")
    require(set(defaults) == COMPONENT_DEFAULT_KEYS, "component default field drift")
    require(defaults.get("branch") == "main", "default branch drift")
    require(defaults.get("source") in SOURCE_STATES, "default source state invalid")
    require(defaults.get("protected") is False, "default protection must fail closed")
    require(defaults.get("checks") in CHECK_STATES, "default checks invalid")
    require(defaults.get("reviewer") in REVIEWER_STATES, "default reviewer invalid")
    require(defaults.get("prs") == [], "default PR list must be empty")
    require(defaults.get("deps") == [], "default dependencies must be empty")
    require(defaults.get("binding") in BINDING_STATES, "default binding invalid")
    require(defaults.get("state") == "BLOCKED", "default production state must be BLOCKED")
    require(
        isinstance(defaults.get("blockers"), list) and defaults["blockers"],
        "default blockers missing",
    )

    components_raw = value.get("components")
    require(
        isinstance(components_raw, list) and len(components_raw) >= 20,
        "component coverage incomplete",
    )
    ids: set[str] = set()
    repositories: set[str] = set()
    repository_ids: set[int] = set()
    components: list[dict[str, Any]] = []
    for raw in components_raw:
        require(isinstance(raw, Mapping), "component must be an object")
        row = expand_component(raw, defaults)
        cid = row.get("id")
        require(isinstance(cid, str) and cid, "component ID missing")
        require(cid not in ids, f"duplicate component ID: {cid}")
        ids.add(cid)

        repository = row.get("repo")
        require(
            isinstance(repository, str) and REPO_RE.fullmatch(repository),
            f"{cid}: invalid repository authority",
        )
        require(repository not in repositories, f"duplicate repository: {repository}")
        repositories.add(repository)
        repository_id = row.get("rid")
        require(
            isinstance(repository_id, int) and repository_id > 0,
            f"{cid}: repository ID invalid",
        )
        require(repository_id not in repository_ids, f"duplicate repository ID: {repository_id}")
        repository_ids.add(repository_id)

        require(
            isinstance(row.get("branch"), str) and row["branch"],
            f"{cid}: authority branch missing",
        )
        require(
            isinstance(row.get("sha"), str) and SHA_RE.fullmatch(row["sha"]),
            f"{cid}: source SHA invalid",
        )
        require(row.get("source") in SOURCE_STATES, f"{cid}: invalid source state")
        require(row.get("state") in PRODUCTION_STATES, f"{cid}: invalid production state")
        require(isinstance(row.get("protected"), bool), f"{cid}: branch protection invalid")
        require(row.get("checks") in CHECK_STATES, f"{cid}: required-check state invalid")
        require(row.get("reviewer") in REVIEWER_STATES, f"{cid}: reviewer state invalid")
        require(row.get("binding") in BINDING_STATES, f"{cid}: binding state invalid")
        blockers = row.get("blockers")
        require(isinstance(blockers, list) and blockers, f"{cid}: blockers must be explicit")
        require(
            all(isinstance(item, str) and item for item in blockers),
            f"{cid}: invalid blocker",
        )

        if row["source"] == "protected_source_ready":
            require(row["protected"] is True, f"{cid}: protected source claim invalid")
            require(row["checks"] == "active", f"{cid}: protected source requires active checks")
        if row["protected"] is False or row["checks"] != "active":
            require(row["state"] == "BLOCKED", f"{cid}: incomplete governance must remain blocked")

        prs = row.get("prs")
        require(isinstance(prs, list), f"{cid}: candidates must be a list")
        for candidate in prs:
            require(isinstance(candidate, Mapping), f"{cid}: candidate must be an object")
            validate_candidate(cid, candidate)
        if row["source"] in {"candidate_pending_review", "candidate_needs_refresh"}:
            require(bool(prs), f"{cid}: candidate source state requires PR evidence")

        deps = row.get("deps")
        require(isinstance(deps, list), f"{cid}: dependencies must be a list")
        require(
            all(isinstance(item, str) and item for item in deps),
            f"{cid}: invalid dependency",
        )
        components.append(row)

    for row in components:
        for dependency in row["deps"]:
            require(dependency in ids, f"{row['id']}: unknown dependency: {dependency}")
            require(dependency != row["id"], f"{row['id']}: self-dependency forbidden")

    certifications = value.get("runtime_certifications")
    require(isinstance(certifications, Mapping), "runtime certifications must be an object")
    by_id = {row["id"]: row for row in components}
    for cid, certification in certifications.items():
        require(cid in by_id, f"unknown runtime certification: {cid}")
        require(isinstance(certification, Mapping), f"{cid}: certification must be an object")
        validate_certification(by_id[cid], certification)

    order = value.get("promotion_order")
    require(isinstance(order, list) and len(order) >= 10, "promotion order incomplete")
    require(all(isinstance(step, str) and step for step in order), "invalid promotion step")

    return {
        "schema_version": "1.0",
        "result": "PASS",
        "decision": "NO_GO",
        "component_count": len(components),
        "runtime_certified_count": len(certifications),
        "production_activated": False,
        "external_effects_enabled": [],
        "calls_placed": 0,
    }


def write_evidence(document: Mapping[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / "validation.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        document = validate_lock(load_lock(args.lock))
        write_evidence(document)
        print(
            "PRODUCTION_INTEGRATION_LOCK=PASS "
            f"components={document['component_count']} "
            f"runtime_certified={document['runtime_certified_count']} "
            "decision=NO_GO calls_placed=0"
        )
        return 0
    except LockError as exc:
        document = {
            "schema_version": "1.0",
            "result": "FAIL",
            "decision": "NO_GO",
            "production_activated": False,
            "calls_placed": 0,
            "error": str(exc),
        }
        write_evidence(document)
        print(f"PRODUCTION_INTEGRATION_LOCK=FAIL reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
